# -*- coding: utf-8 -*-
#
# Copyright © 2011-2013 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public
# License as published by the Free Software Foundation; either version
# 2 of the License (GPLv2) or (at your option) any later version.
# There is NO WARRANTY for this software, express or implied,
# including the implied warranties of MERCHANTABILITY,
# NON-INFRINGEMENT, or FITNESS FOR A PARTICULAR PURPOSE. You should
# have received a copy of GPLv2 along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.

from ConfigParser import SafeConfigParser
import gettext
import os
import re
import shutil
import time
import traceback

from pulp.plugins.distributor import Distributor
from pulp.server.config import config as pulp_server_config
from pulp.server.db.model.criteria import UnitAssociationCriteria

from pulp_rpm.common.ids import (TYPE_ID_DISTRO, TYPE_ID_DRPM, TYPE_ID_ERRATA, TYPE_ID_PKG_GROUP,
                                 TYPE_ID_PKG_CATEGORY, TYPE_ID_RPM, TYPE_ID_SRPM, TYPE_ID_DISTRIBUTOR_YUM,
                                 TYPE_ID_YUM_REPO_METADATA_FILE)
from pulp_rpm.repo_auth import protected_repo_utils, repo_cert_utils
from pulp_rpm.yum_plugin import comps_util, util, metadata, updateinfo
from pulp_rpm.plugins.importers.yum.parse.treeinfo import KEY_PACKAGEDIR
import pulp_rpm.common.constants as constants

# -- constants ----------------------------------------------------------------

_LOG = util.getLogger(__name__)
_ = gettext.gettext

REQUIRED_CONFIG_KEYS = ["relative_url", "http", "https"]
OPTIONAL_CONFIG_KEYS = ["protected", "auth_cert", "auth_ca", "https_ca", "gpgkey",  "checksum_type",
                        "skip", "https_publish_dir", "http_publish_dir", "use_createrepo", "skip_pkg_tags"]

SUPPORTED_UNIT_TYPES = [TYPE_ID_RPM, TYPE_ID_SRPM, TYPE_ID_DRPM, TYPE_ID_DISTRO]
HTTP_PUBLISH_DIR="/var/lib/pulp/published/http/repos"
HTTPS_PUBLISH_DIR="/var/lib/pulp/published/https/repos"

OLD_REL_PATH_KEYWORD = 'old_relative_path'

# This needs to be a config option in the distributor's .conf file. But for 2.0,
# I don't have time to add that and realistically, people won't be reconfiguring
# it anyway. This is to replace having it in Pulp's server.conf, which definitely
# isn't the place for it.
RELATIVE_URL = '/pulp/repos'

###
# Config Options Explained
###
# relative_url          - Relative URL to publish
#                         example: relative_url="rhel_6.2" may translate to publishing at
#                         http://localhost/pulp/repos/rhel_6.2
# http                  - True/False:  Publish through http
# https                 - True/False:  Publish through https
# protected             - True/False: Protect this repo with repo authentication
# auth_cert             - Certificate to use if repo authorization is required
# auth_ca               - CA to use if repo authorization is required
# https_ca              - CA to verify https communication
# gpgkey                - GPG Key associated with the packages in this repo
# use_createrepo        - This is  mostly a debug flag to override default snippet based metadata generation with createrepo
#                         False will not run and uses existing metadata from sync
# checksum_type         - Checksum type to use for metadata generation
# skip                  - List of what content types to skip during sync, options:
#                         ["rpm", "drpm", "errata", "distribution", "packagegroup"]
# https_publish_dir     - Optional parameter to override the HTTPS_PUBLISH_DIR, mainly used for unit tests
# http_publish_dir      - Optional parameter to override the HTTP_PUBLISH_DIR, mainly used for unit tests
# TODO:  Need to think some more about a 'mirror' option, how do we want to handle
# mirroring a remote url and not allowing any changes, what we were calling 'preserve_metadata' in v1.
#
# -- plugins ------------------------------------------------------------------

#
# TODO:
#   - Need an unpublish to remove a link, think when a repo is deleted we still keep the symlink under the published dir
#   - Is this really a YumDistributor or should it be a HttpsDistributor?
#   - What if the relative_url changes between invocations,
#    - How will we handle cleanup of the prior publish path/symlink
class YumDistributor(Distributor):

    def __init__(self):
        super(YumDistributor, self).__init__()
        self.canceled = False
        self.use_createrepo = False
        self.package_dir = None

    @classmethod
    def metadata(cls):
        return {
            'id'           : TYPE_ID_DISTRIBUTOR_YUM,
            'display_name' : 'Yum Distributor',
            'types'        : [TYPE_ID_RPM, TYPE_ID_SRPM, TYPE_ID_DRPM, TYPE_ID_ERRATA,
                              TYPE_ID_DISTRO, TYPE_ID_PKG_CATEGORY, TYPE_ID_PKG_GROUP,
                              TYPE_ID_YUM_REPO_METADATA_FILE]
        }

    def validate_config(self, repo, config, config_conduit):
        """
        Validate the distributor config. A tuple of status, msg will be returned. Status indicates success or failure
        with True/False values, and in the event of failure, msg will contain an error message.

        :param repo:          The repo that the config is for
        :type  repo:          pulp.server.db.model.repository.Repo
        :param config:        The configuration to be validated
        :type  config:        pulp.server.content.plugins.config.PluginCallConfiguration
        :param config_conduit: Configuration Conduit;
        :type  config_conduit: pulp.plugins.conduits.repo_validate.RepoConfigConduit
        :return:              tuple of status, message
        :rtype:               tuple
        """
        auth_cert_bundle = {}
        for key in REQUIRED_CONFIG_KEYS:
            value = config.get(key)
            if value is None:
                msg = _("Missing required configuration key: %(key)s" % {"key":key})
                _LOG.error(msg)
                return False, msg
            if key == 'relative_url':
                relative_path = config.get('relative_url')
                if relative_path is not None:
                    if not isinstance(relative_path, basestring):
                        msg = _("relative_url should be a basestring; got %s instead" % relative_path)
                        _LOG.error(msg)
                        return False, msg
                    if re.match('[^a-zA-Z0-9/_-]+', relative_path):
                        msg = _('relative_url must contain only alphanumerics, underscores, and dashes.')
                        _LOG.error(msg)
                        return False, msg
            if key == 'http':
                config_http = config.get('http')
                if config_http is not None and not isinstance(config_http, bool):
                    msg = _("http should be a boolean; got %s instead" % config_http)
                    _LOG.error(msg)
                    return False, msg
            if key == 'https':
                config_https = config.get('https')
                if config_https is not None and not isinstance(config_https, bool):
                    msg = _("https should be a boolean; got %s instead" % config_https)
                    _LOG.error(msg)
                    return False, msg
        for key in config.keys():
            if key not in REQUIRED_CONFIG_KEYS and key not in OPTIONAL_CONFIG_KEYS:
                msg = _("Configuration key '%(key)s' is not supported" % {"key":key})
                _LOG.error(msg)
                return False, msg
            if key == 'protected':
                protected = config.get('protected')
                if not isinstance(protected, bool):
                    msg = _("protected should be a boolean; got %s instead" % protected)
                    _LOG.error(msg)
                    return False, msg
            if key == 'use_createrepo':
                use_createrepo = config.get('use_createrepo')
                if not isinstance(use_createrepo, bool):
                    msg = _("use_createrepo should be a boolean; got %s instead" % use_createrepo)
                    _LOG.error(msg)
                    return False, msg
            if key == 'checksum_type':
                checksum_type = config.get('checksum_type')
                if checksum_type is not None and not util.is_valid_checksum_type(checksum_type):
                    msg = _("%s is not a valid checksum type" % checksum_type)
                    _LOG.error(msg)
                    return False, msg
            if key == 'skip':
                metadata_types = config.get('skip')
                if not isinstance(metadata_types, list):
                    msg = _("skip should be a dictionary; got %s instead" % metadata_types)
                    _LOG.error(msg)
                    return False, msg
            if key == 'auth_cert':
                auth_pem = config.get('auth_cert').encode('utf-8')
                if auth_pem is not None and not util.validate_cert(auth_pem):
                    msg = _("auth_cert is not a valid certificate")
                    _LOG.error(msg)
                    return False, msg
                auth_cert_bundle['cert'] = auth_pem
            if key == 'auth_ca':
                auth_ca = config.get('auth_ca').encode('utf-8')
                if auth_ca is not None and not util.validate_cert(auth_ca):
                    msg = _("auth_ca is not a valid certificate")
                    _LOG.error(msg)
                    return False, msg
                auth_cert_bundle['ca'] = auth_ca
        # process auth certs
        repo_relative_path = self.get_repo_relative_path(repo, config)
        if repo_relative_path.startswith("/"):
            repo_relative_path = repo_relative_path[1:]
        self.process_repo_auth_certificate_bundle(repo.id, repo_relative_path, auth_cert_bundle)
        # If overriding https publish dir, be sure it exists and we can write to it
        publish_dir = config.get("https_publish_dir")
        if publish_dir:
            if not os.path.exists(publish_dir) or not os.path.isdir(publish_dir):
                msg = _("Value for 'https_publish_dir' is not an existing directory: %(publish_dir)s" % {"publish_dir":publish_dir})
                return False, msg
            if not os.access(publish_dir, os.R_OK) or not os.access(publish_dir, os.W_OK):
                msg = _("Unable to read & write to specified 'https_publish_dir': %(publish_dir)s" % {"publish_dir":publish_dir})
                return False, msg
        publish_dir = config.get("http_publish_dir")
        if publish_dir:
            if not os.path.exists(publish_dir) or not os.path.isdir(publish_dir):
                msg = _("Value for 'http_publish_dir' is not an existing directory: %(publish_dir)s" % {"publish_dir":publish_dir})
                return False, msg
            if not os.access(publish_dir, os.R_OK) or not os.access(publish_dir, os.W_OK):
                msg = _("Unable to read & write to specified 'http_publish_dir': %(publish_dir)s" % {"publish_dir":publish_dir})
                return False, msg
        conflict_items = config_conduit.get_repo_distributors_by_relative_url(repo_relative_path, repo.id)
        if conflict_items.count() > 0:
            item = next(conflict_items)
            conflicting_url = item["repo_id"]
            if item['config']["relative_url"] is not None:
                conflicting_url = item['config']["relative_url"]
            conflict_msg = _("Relative url '%(rel_url)s' conflicts with existing relative_url of '%(conflict_rel_url)s' from repo '%(conflict_repo_id)s'" \
            % {"rel_url": repo_relative_path, "conflict_rel_url": conflicting_url, "conflict_repo_id": item["repo_id"]})
            _LOG.info(conflict_msg)
            return False, conflict_msg

        return True, None

    def init_progress(self):
        return  {
            "state": "IN_PROGRESS",
            "num_success" : 0,
            "num_error" : 0,
            "items_left" : 0,
            "items_total" : 0,
            "error_details" : [],
        }

    def process_repo_auth_certificate_bundle(self, repo_id, repo_relative_path, cert_bundle):
        """
        Write the cert bundle to location specified in the repo_auth.conf;
        also updates the protected_repo_listings file with repo info. If
        no cert bundle, delete any orphaned repo info from listings.

        @param repo_id: repository id
        @type repo_id: str

        @param repo_relative_path: repo relative path
        @type  repo_relative_path: str

        @param cert_bundle: mapping of item to its PEM encoded contents
        @type  cert_bundle: dict {str, str}
        """

        repo_auth_config = load_config()
        repo_cert_utils_obj = repo_cert_utils.RepoCertUtils(repo_auth_config)
        protected_repo_utils_obj = protected_repo_utils.ProtectedRepoUtils(repo_auth_config)

        if cert_bundle:
            repo_cert_utils_obj.write_consumer_cert_bundle(repo_id, cert_bundle)
            # add repo to protected list
            protected_repo_utils_obj.add_protected_repo(repo_relative_path, repo_id)
        else:
            # remove stale info, if any
            protected_repo_utils_obj.delete_protected_repo(repo_relative_path)

    def get_http_publish_dir(self, config=None):
        """
        @param config
        @type pulp.server.content.plugins.config.PluginCallConfiguration
        """
        if config:
            publish_dir = config.get("http_publish_dir")
            if publish_dir:
                _LOG.info("Override HTTP publish directory from passed in config value to: %s" % (publish_dir))
                return publish_dir
        return HTTP_PUBLISH_DIR

    def get_https_publish_dir(self, config=None):
        """
        @param config
        @type pulp.server.content.plugins.config.PluginCallConfiguration
        """
        if config:
            publish_dir = config.get("https_publish_dir")
            if publish_dir:
                _LOG.info("Override HTTPS publish directory from passed in config value to: %s" % (publish_dir))
                return publish_dir
        return HTTPS_PUBLISH_DIR

    def get_repo_relative_path(self, repo, config):
        relative_url = config.get("relative_url")
        if relative_url:
            return relative_url
        return repo.id

    def cancel_publish_repo(self, call_report, call_request):
        self.canceled = True
        if self.use_createrepo:
            return metadata.cancel_createrepo(self.repo_working_dir)

    def publish_repo(self, repo, publish_conduit, config):
        summary = {}
        details = {}
        progress_status = {
            "packages":           {"state": "NOT_STARTED"},
            "distribution":       {"state": "NOT_STARTED"},
            "metadata":           {"state": "NOT_STARTED"},
            "packagegroups":      {"state": "NOT_STARTED"},
            "publish_http":       {"state": "NOT_STARTED"},
            "publish_https":      {"state": "NOT_STARTED"},
            }

        def progress_callback(type_id, status):
            progress_status[type_id] = status
            publish_conduit.set_progress(progress_status)

        self.repo_working_dir = repo.working_dir

        if self.canceled:
            return publish_conduit.build_cancel_report(summary, details)
        skip_list = config.get('skip') or []
        # Determine Content in this repo

        distro_errors = []
        distro_units =  []
        if 'distribution' not in skip_list:
            criteria = UnitAssociationCriteria(type_ids=TYPE_ID_DISTRO)
            distro_units = publish_conduit.get_units(criteria=criteria)
            # symlink distribution files if any under repo.working_dir
            distro_status, distro_errors = self.symlink_distribution_unit_files(distro_units, repo.working_dir, publish_conduit, progress_callback)
            if not distro_status:
                _LOG.error("Unable to publish distribution tree %s items" % (len(distro_errors)))

        pkg_units = []
        pkg_errors = []
        if 'rpm' not in skip_list:
            for type_id in [TYPE_ID_RPM, TYPE_ID_SRPM]:
                criteria = UnitAssociationCriteria(type_ids=type_id,
                    unit_fields=['id', 'name', 'version', 'release', 'arch', 'epoch', '_storage_path', "checksum", "checksumtype" ])
                pkg_units += publish_conduit.get_units(criteria=criteria)
            drpm_units = []
            if 'drpm' not in skip_list:
                criteria = UnitAssociationCriteria(type_ids=TYPE_ID_DRPM)
                drpm_units = publish_conduit.get_units(criteria=criteria)
            pkg_units += drpm_units
            # Create symlinks under repo.working_dir
            pkg_status, pkg_errors = self.handle_symlinks(pkg_units, repo.working_dir, progress_callback)
            if not pkg_status:
                _LOG.error("Unable to publish %s items" % (len(pkg_errors)))

        updateinfo_xml_path = None
        if 'erratum' not in skip_list:
            criteria = UnitAssociationCriteria(type_ids=TYPE_ID_ERRATA)
            errata_units = publish_conduit.get_units(criteria=criteria)
            updateinfo_xml_path = updateinfo.updateinfo(errata_units, repo.working_dir)

        if self.canceled:
            return publish_conduit.build_cancel_report(summary, details)
        groups_xml_path = None
        existing_cats = []
        existing_groups = []
        if 'packagegroup' not in skip_list:
            criteria = UnitAssociationCriteria(type_ids=[TYPE_ID_PKG_GROUP, TYPE_ID_PKG_CATEGORY])
            existing_units = publish_conduit.get_units(criteria)
            existing_groups = filter(lambda u : u.type_id in [TYPE_ID_PKG_GROUP], existing_units)
            existing_cats = filter(lambda u : u.type_id in [TYPE_ID_PKG_CATEGORY], existing_units)
            groups_xml_path = comps_util.write_comps_xml(repo.working_dir, existing_groups, existing_cats)
        metadata_start_time = time.time()
        # update/generate metadata for the published repo
        self.use_createrepo = config.get('use_createrepo')
        if self.use_createrepo:
            metadata_status, metadata_errors = metadata.generate_metadata(
                repo.working_dir, publish_conduit, config, progress_callback, groups_xml_path)
        else:
            metadata_status, metadata_errors = metadata.generate_yum_metadata(repo.id, repo.working_dir, publish_conduit, config,
                progress_callback, is_cancelled=self.canceled, group_xml_path=groups_xml_path, updateinfo_xml_path=updateinfo_xml_path,
                repo_scratchpad=publish_conduit.get_repo_scratchpad())

        metadata_end_time = time.time()
        relpath = self.get_repo_relative_path(repo, config)
        if relpath.startswith("/"):
            relpath = relpath[1:]

        # Build the https and http publishing paths
        https_publish_dir = self.get_https_publish_dir(config)
        https_repo_publish_dir = os.path.join(https_publish_dir, relpath).rstrip('/')
        http_publish_dir = self.get_http_publish_dir(config)
        http_repo_publish_dir = os.path.join(http_publish_dir, relpath).rstrip('/')

        # Clean up the old publish directories, if they exist.
        scratchpad = publish_conduit.get_repo_scratchpad()
        if OLD_REL_PATH_KEYWORD in scratchpad:
            old_relative_path = scratchpad[OLD_REL_PATH_KEYWORD]
            old_https_repo_publish_dir = os.path.join(https_publish_dir, old_relative_path)
            if os.path.exists(old_https_repo_publish_dir):
                util.remove_repo_publish_dir(https_publish_dir, old_https_repo_publish_dir)
            old_http_repo_publish_dir = os.path.join(http_publish_dir, old_relative_path)
            if os.path.exists(old_http_repo_publish_dir):
                util.remove_repo_publish_dir(http_publish_dir, old_http_repo_publish_dir)

        # Now write the current publish relative path to the scratch pad. This way, if the relative path
        # changes before the next publish, we can clean up the old path.
        scratchpad[OLD_REL_PATH_KEYWORD] = relpath
        publish_conduit.set_repo_scratchpad(scratchpad)

        # Handle publish link for HTTPS
        if config.get("https"):
            # Publish for HTTPS
            self.set_progress("publish_https", {"state" : "IN_PROGRESS"}, progress_callback)
            try:
                _LOG.info("HTTPS Publishing repo <%s> to <%s>" % (repo.id, https_repo_publish_dir))
                util.create_symlink(repo.working_dir, https_repo_publish_dir)
                util.generate_listing_files(https_publish_dir, https_repo_publish_dir)
                summary["https_publish_dir"] = https_repo_publish_dir
                self.set_progress("publish_https", {"state" : "FINISHED"}, progress_callback)
            except:
                self.set_progress("publish_https", {"state" : "FAILED"}, progress_callback)
        else:
            self.set_progress("publish_https", {"state" : "SKIPPED"}, progress_callback)
            if os.path.lexists(https_repo_publish_dir):
                _LOG.debug("Removing link for %s since https is not set" % https_repo_publish_dir)
                util.remove_repo_publish_dir(https_publish_dir, https_repo_publish_dir)

        # Handle publish link for HTTP
        if config.get("http"):
            # Publish for HTTP
            self.set_progress("publish_http", {"state" : "IN_PROGRESS"}, progress_callback)
            try:
                _LOG.info("HTTP Publishing repo <%s> to <%s>" % (repo.id, http_repo_publish_dir))
                util.create_symlink(repo.working_dir, http_repo_publish_dir)
                util.generate_listing_files(http_publish_dir, http_repo_publish_dir)
                summary["http_publish_dir"] = http_repo_publish_dir
                self.set_progress("publish_http", {"state" : "FINISHED"}, progress_callback)
            except:
                self.set_progress("publish_http", {"state" : "FAILED"}, progress_callback)
        else:
            self.set_progress("publish_http", {"state" : "SKIPPED"}, progress_callback)
            if os.path.lexists(http_repo_publish_dir):
                _LOG.debug("Removing link for %s since http is not set" % http_repo_publish_dir)
                util.remove_repo_publish_dir(http_publish_dir, http_repo_publish_dir)

        summary["num_package_units_attempted"] = len(pkg_units)
        summary["num_package_units_published"] = len(pkg_units) - len(pkg_errors)
        summary["num_package_units_errors"] = len(pkg_errors)
        summary["num_distribution_units_attempted"] = len(distro_units)
        summary["num_distribution_units_published"] = len(distro_units) - len(distro_errors)
        summary["num_distribution_units_errors"] = len(distro_errors)
        summary["num_package_groups_published"] = len(existing_groups)
        summary["num_package_categories_published"] = len(existing_cats)
        summary["relative_path"] = relpath
        if metadata_status is False and not len(metadata_errors):
            summary["skip_metadata_update"] = True
        else:
            summary["skip_metadata_update"] = False
        details["errors"] = pkg_errors + distro_errors # metadata_errors
        details['time_metadata_sec'] = metadata_end_time - metadata_start_time
        # metadata generate skipped vs run
        _LOG.info("Publish complete:  summary = <%s>, details = <%s>" % (summary, details))
        if details["errors"]:
            return publish_conduit.build_failure_report(summary, details)
        return publish_conduit.build_success_report(summary, details)

    def distributor_removed(self, repo, config):
        # clean up any repo specific data
        repo_auth_config = load_config()
        repo_cert_utils_obj = repo_cert_utils.RepoCertUtils(repo_auth_config)
        protected_repo_utils_obj = protected_repo_utils.ProtectedRepoUtils(repo_auth_config)
        repo_relative_path = self.get_repo_relative_path(repo, config)
        if repo_relative_path.startswith("/"):
            repo_relative_path = repo_relative_path[1:]
        repo_cert_utils_obj.delete_for_repo(repo.id)
        protected_repo_utils_obj.delete_protected_repo(repo_relative_path)

        # Clean up https and http publishing paths, if they exist
        https_publish_dir = self.get_https_publish_dir(config)
        https_repo_publish_dir = os.path.join(https_publish_dir, repo_relative_path).rstrip('/')
        http_publish_dir = self.get_http_publish_dir(config)
        http_repo_publish_dir = os.path.join(http_publish_dir, repo_relative_path).rstrip('/')
        if os.path.exists(https_repo_publish_dir):
            util.remove_repo_publish_dir(https_publish_dir, https_repo_publish_dir)
        if os.path.exists(http_repo_publish_dir):
            util.remove_repo_publish_dir(http_publish_dir, http_repo_publish_dir)

    def set_progress(self, type_id, status, progress_callback=None):
        if progress_callback:
            progress_callback(type_id, status)

    def handle_symlinks(self, units, symlink_dir, progress_callback=None):
        """
        @param units list of units that belong to the repo and should be published
        @type units [AssociatedUnit]

        @param symlink_dir where to create symlinks
        @type symlink_dir str

        @param progress_callback: callback to report progress info to publish_conduit
        @type  progress_callback: function

        @return tuple of status and list of error messages if any occurred
        @rtype (bool, [str])
        """
        packages_progress_status = self.init_progress()
        self.set_progress("packages", packages_progress_status, progress_callback)
        errors = []
        packages_progress_status["items_total"] = len(units)
        packages_progress_status["items_left"] =  len(units)
        for u in units:
            self.set_progress("packages", packages_progress_status, progress_callback)
            relpath = util.get_relpath_from_unit(u)
            source_path = u.storage_path
            symlink_path = os.path.join(symlink_dir, relpath)
            if not os.path.exists(source_path):
                msg = "Source path: %s is missing" % (source_path)
                errors.append((source_path, symlink_path, msg))
                packages_progress_status["num_error"] += 1
                packages_progress_status["items_left"] -= 1
                continue
            _LOG.debug("Unit exists at: %s we need to symlink to: %s" % (source_path, symlink_path))
            try:
                if not util.create_symlink(source_path, symlink_path):
                    msg = _("Unable to create symlink for: %(symlink_path)s"
                            " pointing to %(source_path)s" % {'symlink_path': symlink_path,
                                                              'source_path': source_path})
                    _LOG.error(msg)
                    errors.append((source_path, symlink_path, msg))
                    packages_progress_status["num_error"] += 1
                    packages_progress_status["items_left"] -= 1
                    continue

                if self.package_dir is not None:
                    symlink_path = os.path.join(symlink_dir, self.package_dir, relpath)
                    if not util.create_symlink(source_path, symlink_path):
                        msg = _("Unable to create symlink for: %(symlink_path)s"
                                " pointing to %(source_path)s" % {'symlink_path': symlink_path,
                                                                  'source_path': source_path})
                        _LOG.error(msg)
                        errors.append((source_path, symlink_path, msg))
                        packages_progress_status["num_error"] += 1
                        packages_progress_status["items_left"] -= 1
                packages_progress_status["num_success"] += 1
            except Exception, e:
                tb_info = traceback.format_exc()
                _LOG.error("%s" % (tb_info))
                _LOG.critical(e)
                errors.append((source_path, symlink_path, str(e)))
                packages_progress_status["num_error"] += 1
                packages_progress_status["items_left"] -= 1
                continue
            packages_progress_status["items_left"] -= 1
        if errors:
            packages_progress_status["error_details"] = errors
            return False, errors
        packages_progress_status["state"] = "FINISHED"
        self.set_progress("packages", packages_progress_status, progress_callback)
        return True, []

    def copy_importer_repodata(self, src_working_dir, tgt_working_dir):
        """
        @param src_working_dir importer repo working dir where repodata dir exists
        @type src_working_dir str

        @param tgt_working_dir importer repo working dir where repodata dir needs to be copied
        @type tgt_working_dir str

        @return True - success, False - error
        @rtype bool
        """
        try:
            src_repodata_dir = os.path.join(src_working_dir, "repodata")
            if not os.path.exists(src_repodata_dir):
                _LOG.debug("No repodata dir to copy at %s" % src_repodata_dir)
                return False
            tgt_repodata_dir = os.path.join(tgt_working_dir, "repodata")
            if os.path.exists(tgt_repodata_dir):
                shutil.rmtree(tgt_repodata_dir)
            shutil.copytree(src_repodata_dir, tgt_repodata_dir)
        except (IOError, OSError):
            _LOG.error("Unable to copy repodata directory from %s to %s" % (src_working_dir, tgt_working_dir))
            tb_info = traceback.format_exc()
            _LOG.error("%s" % (tb_info))
            return False
        _LOG.info("Copied repodata from %s to %s" % (src_working_dir, tgt_working_dir))
        return True

    def symlink_distribution_unit_files(self, units, symlink_dir, publish_conduit, progress_callback=None):
        """
        Publishing distriubution unit involves publishing files underneath the unit.
        Distribution is an aggregate unit with distribution files. This call
        looksup each distribution unit and symlinks the files from the storage location
        to working directory.

        @param units
        @type AssociatedUnit

        @param symlink_dir: path of where we want the symlink to reside
        @type symlink_dir str

        @param progress_callback: callback to report progress info to publish_conduit
        @type  progress_callback: function

        @return tuple of status and list of error messages if any occurred
        @rtype (bool, [str])
        """
        distro_progress_status = self.init_progress()
        self.set_progress("distribution", distro_progress_status, progress_callback)
        _LOG.debug("Process symlinking distribution files with %s units to %s dir" % (len(units), symlink_dir))
        # handle orphaned
        existing_scratchpad = publish_conduit.get_scratchpad() or {}
        scratchpad = self._handle_orphaned_distributions(units, symlink_dir, existing_scratchpad)
        errors = []
        for u in units:
            source_path_dir = u.storage_path
            if KEY_PACKAGEDIR in u.metadata and u.metadata[KEY_PACKAGEDIR] is not None:
                self.package_dir = u.metadata[KEY_PACKAGEDIR]
                package_path = os.path.join(symlink_dir, self.package_dir)
                if os.path.islink(package_path):
                    # a package path exists as a symlink we are going to remove it since this
                    # will create a real directory
                    os.unlink(package_path)
                if not os.path.exists(package_path):
                    os.makedirs(package_path)
            if not 'files' in u.metadata:
                msg = _("No distribution files found for unit %s" % u)
                _LOG.error(msg)
            distro_files = u.metadata['files']
            _LOG.debug("Found %s distribution files to symlink" % len(distro_files))
            distro_progress_status['items_total'] = len(distro_files)
            distro_progress_status['items_left'] = len(distro_files)
            # Lookup treeinfo file in the source location
            src_treeinfo_path = None
            for treeinfo in constants.TREE_INFO_LIST:
                src_treeinfo_path = os.path.join(source_path_dir, treeinfo)
                if os.path.exists(src_treeinfo_path):
                    # we found the treeinfo file
                    break
            if src_treeinfo_path is not None:
                # create a symlink from content location to repo location.
                symlink_treeinfo_path = os.path.join(symlink_dir, treeinfo)
                _LOG.debug("creating treeinfo symlink from %s to %s" % (src_treeinfo_path, symlink_treeinfo_path))
                util.create_symlink(src_treeinfo_path, symlink_treeinfo_path)
            published_distro_files = []
            for dfile in distro_files:
                self.set_progress("distribution", distro_progress_status, progress_callback)
                source_path = os.path.join(source_path_dir, dfile['relativepath'])
                symlink_path = os.path.join(symlink_dir, dfile['relativepath'])
                if os.path.exists(symlink_path):
                    # path already exists, skip symlink
                    distro_progress_status["items_left"] -= 1
                    published_distro_files.append(symlink_path)
                    continue
                if not os.path.exists(source_path):
                    msg = "Source path: %s is missing" % source_path
                    errors.append((source_path, symlink_path, msg))
                    distro_progress_status['num_error'] += 1
                    distro_progress_status["items_left"] -= 1
                    continue
                try:
                    if not util.create_symlink(source_path, symlink_path):
                        msg = "Unable to create symlink for: %s pointing to %s" % (symlink_path, source_path)
                        _LOG.error(msg)
                        errors.append((source_path, symlink_path, msg))
                        distro_progress_status['num_error'] += 1
                        distro_progress_status["items_left"] -= 1
                        continue
                    distro_progress_status['num_success'] += 1
                    published_distro_files.append(symlink_path)
                except Exception, e:
                    tb_info = traceback.format_exc()
                    _LOG.error("%s" % tb_info)
                    _LOG.critical(e)
                    errors.append((source_path, symlink_path, str(e)))
                    distro_progress_status['num_error'] += 1
                    distro_progress_status["items_left"] -= 1
                    continue
                distro_progress_status["items_left"] -= 1
            scratchpad.update({constants.PUBLISHED_DISTRIBUTION_FILES_KEY : {u.id : published_distro_files}})
        # create the Packages symlink to the content dir, in the content dir
        packages_symlink_path = os.path.join(symlink_dir, 'Packages')
        if not os.path.exists(packages_symlink_path) and not util.create_symlink(symlink_dir, packages_symlink_path):
            msg = 'Unable to create Packages symlink required for RHEL 5 distributions'
            _LOG.error(msg)
            errors.append((symlink_dir, packages_symlink_path, msg))
        publish_conduit.set_scratchpad(scratchpad)
        if errors:
            distro_progress_status["error_details"] = errors
            distro_progress_status["state"] = "FAILED"
            self.set_progress("distribution", distro_progress_status, progress_callback)
            return False, errors
        distro_progress_status["state"] = "FINISHED"
        self.set_progress("distribution", distro_progress_status, progress_callback)
        return True, []

    def _handle_orphaned_distributions(self, units, repo_working_dir, scratchpad):
        distro_unit_ids = [u.id for u in units]
        published_distro_units = scratchpad.get(constants.PUBLISHED_DISTRIBUTION_FILES_KEY, [])
        for distroid in published_distro_units:
            if distroid not in distro_unit_ids:
                # distro id on scratchpad not in the repo; remove the associated symlinks
                for orphaned_path in published_distro_units[distroid]:
                    if os.path.islink(orphaned_path):
                        _LOG.debug("cleaning up orphaned distribution path %s" % orphaned_path)
                        util.remove_repo_publish_dir(repo_working_dir, orphaned_path)
                    # remove the cleaned up distroid from scratchpad
                del scratchpad[constants.PUBLISHED_DISTRIBUTION_FILES_KEY][distroid]
        return scratchpad

    def create_consumer_payload(self, repo, config, binding_config):
        payload = {}
        ##TODO for jdob: load the pulp.conf and make it accessible to distributor
        payload['repo_name'] = repo.display_name
        payload['server_name'] = pulp_server_config.get('server', 'server_name')
        ssl_ca_path = pulp_server_config.get('security', 'ssl_ca_certificate')
        if os.path.exists(ssl_ca_path):
            payload['ca_cert'] = open(pulp_server_config.get('security', 'ssl_ca_certificate')).read()
        else:
            payload['ca_cert'] = config.get('https_ca')
        payload['relative_path'] = \
            '/'.join((RELATIVE_URL,
                      self.get_repo_relative_path(repo, config)))
        payload['protocols'] = []
        if config.get('http'):
            payload['protocols'].append('http')
        if config.get('https'):
            payload['protocols'].append('https')
        payload['gpg_keys'] = []
        if config.get('gpgkey') is not None:
            payload['gpg_keys'] = {'pulp.key': config.get('gpgkey')}
        payload['client_cert'] = None
        if config.get('auth_cert') and config.get('auth_ca'):
            payload['client_cert'] = config.get('auth_cert')
        else:
            # load the global auth if enabled
            repo_auth_config = load_config()
            global_cert_dir =  repo_auth_config.get('repos', 'global_cert_location')
            global_auth_cert = os.path.join(global_cert_dir, 'pulp-global-repo.cert')
            global_auth_key = os.path.join(global_cert_dir, 'pulp-global-repo.key')
            global_auth_ca = os.path.join(global_cert_dir, 'pulp-global-repo.ca')
            if os.path.exists(global_auth_ca) and os.path.exists(global_auth_cert):
                payload['global_auth_cert'] = open(global_auth_cert).read()
                payload['global_auth_key'] = open(global_auth_key).read()
                payload['global_auth_ca'] = open(global_auth_ca).read()
        return payload

# -- local utility functions ---------------------------------------------------

def load_config(config_file=constants.REPO_AUTH_CONFIG_FILE):
    config = SafeConfigParser()
    config.read(config_file)
    return config
