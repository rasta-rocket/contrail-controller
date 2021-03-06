#
# Copyright (c) 2013 Juniper Networks, Inc. All rights reserved.
#
"""
This is the main module in vnc_cfg_api_server package. It manages interaction
between http/rest, address management, authentication and database interfaces.
"""

from gevent import monkey
monkey.patch_all()
from gevent import hub

# from neutron plugin to api server, the request URL could be large.
# fix the const
import gevent.pywsgi
gevent.pywsgi.MAX_REQUEST_LINE = 65535

import sys
reload(sys)
sys.setdefaultencoding('UTF8')
import ConfigParser
import functools
import hashlib
import logging
import logging.config
import signal
import os
import re
import random
import socket
from cfgm_common import jsonutils as json
from provision_defaults import *
import uuid
import copy
from pprint import pformat
from cStringIO import StringIO
# import GreenletProfiler

from cfgm_common import vnc_cgitb
from cfgm_common import has_role

logger = logging.getLogger(__name__)

"""
Following is needed to silence warnings on every request when keystone
    auth_token middleware + Sandesh is used. Keystone or Sandesh alone
    do not produce these warnings.

Exception AttributeError: AttributeError(
    "'_DummyThread' object has no attribute '_Thread__block'",)
    in <module 'threading' from '/usr/lib64/python2.7/threading.pyc'> ignored

See http://stackoverflow.com/questions/13193278/understand-python-threading-bug
for more information.
"""
import threading
threading._DummyThread._Thread__stop = lambda x: 42

CONFIG_VERSION = '1.0'

import bottle
bottle.BaseRequest.MEMFILE_MAX = 1024000

import utils
import context
from context import get_request, get_context, set_context, use_context
from context import ApiContext
import vnc_cfg_types
from vnc_db import VncDbClient

import cfgm_common
from cfgm_common import ignore_exceptions
from cfgm_common.uve.vnc_api.ttypes import VncApiCommon, VncApiConfigLog,\
    VncApiDebug, VncApiInfo, VncApiNotice, VncApiError
from cfgm_common import illegal_xml_chars_RE
from sandesh_common.vns.ttypes import Module
from sandesh_common.vns.constants import ModuleNames, Module2NodeType,\
    NodeTypeNames, INSTANCE_ID_DEFAULT

from provision_defaults import Provision
from vnc_quota import *
from gen.resource_xsd import *
from gen.resource_common import *
from gen.vnc_api_client_gen import all_resource_type_tuples
import cfgm_common
from cfgm_common.utils import cgitb_hook
from cfgm_common.rest import LinkObject, hdr_server_tenant
from cfgm_common.exceptions import *
from cfgm_common.vnc_extensions import ExtensionManager
import gen.resource_xsd
import vnc_addr_mgmt
import vnc_auth
import vnc_auth_keystone
import vnc_perms
import vnc_rbac
from cfgm_common.uve.cfgm_cpuinfo.ttypes import ModuleCpuState, ModuleCpuStateTrace
from cfgm_common.buildinfo import build_info
from cfgm_common.vnc_api_stats import log_api_stats

from pysandesh.sandesh_base import *
from pysandesh.gen_py.sandesh.ttypes import SandeshLevel
# from gen_py.vnc_api.ttypes import *
import netifaces
from pysandesh.connection_info import ConnectionState
from cfgm_common.uve.nodeinfo.ttypes import NodeStatusUVE, \
    NodeStatus

from sandesh.traces.ttypes import RestApiTrace
from vnc_bottle import get_bottle_server
from cfgm_common.vnc_greenlets import VncGreenlet

_ACTION_RESOURCES = [
    {'uri': '/prop-collection-get', 'link_name': 'prop-collection-get',
     'method': 'GET', 'method_name': 'prop_collection_http_get'},
    {'uri': '/prop-collection-update', 'link_name': 'prop-collection-update',
     'method': 'POST', 'method_name': 'prop_collection_http_post'},
    {'uri': '/ref-update', 'link_name': 'ref-update',
     'method': 'POST', 'method_name': 'ref_update_http_post'},
    {'uri': '/ref-relax-for-delete', 'link_name': 'ref-relax-for-delete',
     'method': 'POST', 'method_name': 'ref_relax_for_delete_http_post'},
    {'uri': '/fqname-to-id', 'link_name': 'name-to-id',
     'method': 'POST', 'method_name': 'fq_name_to_id_http_post'},
    {'uri': '/id-to-fqname', 'link_name': 'id-to-name',
     'method': 'POST', 'method_name': 'id_to_fq_name_http_post'},
    {'uri': '/useragent-kv', 'link_name': 'useragent-keyvalue',
     'method': 'POST', 'method_name': 'useragent_kv_http_post'},
    {'uri': '/db-check', 'link_name': 'database-check',
     'method': 'POST', 'method_name': 'db_check'},
    {'uri': '/fetch-records', 'link_name': 'fetch-records',
     'method': 'POST', 'method_name': 'fetch_records'},
    {'uri': '/start-profile', 'link_name': 'start-profile',
     'method': 'POST', 'method_name': 'start_profile'},
    {'uri': '/stop-profile', 'link_name': 'stop-profile',
     'method': 'POST', 'method_name': 'stop_profile'},
    {'uri': '/list-bulk-collection', 'link_name': 'list-bulk-collection',
     'method': 'POST', 'method_name': 'list_bulk_collection_http_post'},
    {'uri': '/obj-perms', 'link_name': 'obj-perms',
     'method': 'GET', 'method_name': 'obj_perms_http_get'},
    {'uri': '/chown', 'link_name': 'chown',
     'method': 'POST', 'method_name': 'obj_chown_http_post'},
    {'uri': '/chmod', 'link_name': 'chmod',
     'method': 'POST', 'method_name': 'obj_chmod_http_post'},
    {'uri': '/multi-tenancy', 'link_name': 'multi-tenancy',
     'method': 'PUT', 'method_name': 'mt_http_put'},
    {'uri': '/aaa-mode', 'link_name': 'aaa-mode',
     'method': 'PUT', 'method_name': 'aaa_mode_http_put'},
]


def error_400(err):
    return err.body
# end error_400


def error_403(err):
    return err.body
# end error_403


def error_404(err):
    return err.body
# end error_404


def error_409(err):
    return err.body
# end error_409

@bottle.error(412)
def error_412(err):
    return err.body
# end error_412

def error_500(err):
    return err.body
# end error_500


def error_503(err):
    return err.body
# end error_503


class VncApiServer(object):
    """
    This is the manager class co-ordinating all classes present in the package
    """
    _INVALID_NAME_CHARS = set(':')
    _GENERATE_DEFAULT_INSTANCE = [
        'namespace',
        'project',
        'virtual_network', 'virtual-network',
        'network_ipam', 'network-ipam',
    ]
    def __new__(cls, *args, **kwargs):
        obj = super(VncApiServer, cls).__new__(cls, *args, **kwargs)
        obj.api_bottle = bottle.Bottle()
        obj.route('/', 'GET', obj.homepage_http_get)
        obj.api_bottle.error_handler = {
                400: error_400,
                403: error_403,
                404: error_404,
                409: error_409,
                500: error_500,
                503: error_503,
            }

        cls._generate_resource_crud_methods(obj)
        cls._generate_resource_crud_uri(obj)
        for act_res in _ACTION_RESOURCES:
            http_method = act_res.get('method', 'POST')
            method_name = getattr(obj, act_res['method_name'])
            obj.route(act_res['uri'], http_method, method_name)
        return obj
    # end __new__

    @classmethod
    def _validate_complex_type(cls, dict_cls, dict_body):
        if dict_body is None:
            return
        for key, value in dict_body.items():
            if key not in dict_cls.attr_fields:
                raise ValueError('class %s does not have field %s' % (
                                  str(dict_cls), key))
            attr_type_vals = dict_cls.attr_field_type_vals[key]
            attr_type = attr_type_vals['attr_type']
            restrictions = attr_type_vals['restrictions']
            is_array = attr_type_vals.get('is_array', False)
            if value is None:
                continue
            if is_array:
                if not isinstance(value, list):
                    raise ValueError('Field %s must be a list. Received value: %s'
                                     % (key, str(value)))
                values = value
            else:
                values = [value]
            if attr_type_vals['is_complex']:
                attr_cls = cfgm_common.utils.str_to_class(attr_type, __name__)
                for item in values:
                    cls._validate_complex_type(attr_cls, item)
            else:
                simple_type = attr_type_vals['simple_type']
                for item in values:
                    cls._validate_simple_type(key, attr_type,
                                              simple_type, item,
                                              restrictions)
    # end _validate_complex_type

    @classmethod
    def _validate_communityattribute_type(cls, value):
        poss_values = ["no-export",
                       "accept-own",
                       "no-advertise",
                       "no-export-subconfed",
                       "no-reoriginate"]
        if value in poss_values:
            return

        res = re.match('[0-9]+:[0-9]+', value)
        if res is None:
            raise ValueError('Invalid community format %s. '
                             'Change to \'number:number\''
                              % value)

        asn = value.split(':')
        if int(asn[0]) > 65535:
            raise ValueError('Out of range ASN value %s. '
                             'ASN values cannot exceed 65535.'
                             % value)

    @classmethod
    def _validate_serviceinterface_type(cls, value):
        poss_values = ["management",
                       "left",
                       "right"]

        if value in poss_values:
            return

        res = re.match('other[0-9]*', value)
        if res is None:
            raise ValueError('Invalid service interface type %s. '
                             'Valid values are: management|left|right|other[0-9]*'
                              % value)

    @classmethod
    def _validate_simple_type(cls, type_name, xsd_type, simple_type, value, restrictions=None):
        if value is None:
            return
        elif xsd_type in ('unsignedLong', 'integer'):
            if not isinstance(value, (int, long)):
                # If value is not an integer, then try to convert it to integer
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise ValueError('%s: integer value expected instead of %s' %(
                        type_name, value))
            if restrictions:
                if not (int(restrictions[0]) <= value <= int(restrictions[1])):
                    raise ValueError('%s: value must be between %s and %s' %(
                                type_name, restrictions[0], restrictions[1]))
        elif xsd_type == 'boolean':
            if not isinstance(value, bool):
                raise ValueError('%s: true/false expected instead of %s' %(
                    type_name, value))
        elif xsd_type == 'string' and simple_type == 'CommunityAttribute':
            cls._validate_communityattribute_type(value)
        elif xsd_type == 'string' and simple_type == 'ServiceInterfaceType':
            cls._validate_serviceinterface_type(value)
        else:
            if not isinstance(value, basestring):
                raise ValueError('%s: string value expected instead of %s' %(
                    type_name, value))
            if restrictions and value not in restrictions:
                raise ValueError('%s: value must be one of %s' % (
                    type_name, str(restrictions)))
        return value
    # end _validate_simple_type

    def _validate_props_in_request(self, resource_class, obj_dict):
        for prop_name in resource_class.prop_fields:
            prop_field_types = resource_class.prop_field_types[prop_name]
            is_simple = not prop_field_types['is_complex']
            prop_type = prop_field_types['xsd_type']
            restrictions = prop_field_types['restrictions']
            simple_type = prop_field_types['simple_type']
            is_list_prop = prop_name in resource_class.prop_list_fields
            is_map_prop = prop_name in resource_class.prop_map_fields

            prop_value = obj_dict.get(prop_name)
            if not prop_value:
                continue

            if is_simple:
                try:
                    obj_dict[prop_name] = self._validate_simple_type(prop_name,
                                              prop_type, simple_type,
                                              prop_value, restrictions)
                except Exception as e:
                    err_msg = 'Error validating property ' + str(e)
                    return False, err_msg
                else:
                    continue

            prop_cls = cfgm_common.utils.str_to_class(prop_type, __name__)
            if isinstance(prop_value, dict):
                try:
                    self._validate_complex_type(prop_cls, prop_value)
                except Exception as e:
                    err_msg = 'Error validating property %s value %s ' %(
                        prop_name, prop_value)
                    err_msg += str(e)
                    return False, err_msg
            else: # complex-type + value isn't dict or wrapped in list or map
                err_msg = 'Error in property %s type %s value of %s ' %(
                    prop_name, prop_cls, prop_value)
                return False, err_msg
        # end for all properties

        return True, ''
    # end _validate_props_in_request

    def _validate_refs_in_request(self, resource_class, obj_dict):
        for ref_name in resource_class.ref_fields:
            ref_fld_types_list = list(resource_class.ref_field_types[ref_name])
            ref_link_type = ref_fld_types_list[1]
            if ref_link_type == 'None':
                continue

            attr_cls = cfgm_common.utils.str_to_class(ref_link_type, __name__)
            for ref_dict in obj_dict.get(ref_name) or []:
                try:
                    self._validate_complex_type(attr_cls, ref_dict['attr'])
                except Exception as e:
                    err_msg = 'Error validating reference %s value %s ' \
                              %(ref_name, ref_dict)
                    err_msg += str(e)
                    return False, err_msg

        return True, ''
    # end _validate_refs_in_request

    def _validate_perms_in_request(self, resource_class, obj_type, obj_dict):
        for ref_name in resource_class.ref_fields:
            for ref in obj_dict.get(ref_name) or []:
                try:
                    ref_uuid = ref['uuid']
                except KeyError:
                    ref_uuid = self._db_conn.fq_name_to_uuid(ref_name[:-5],
                                                             ref['to'])
                (ok, status) = self._permissions.check_perms_link(
                    get_request(), ref_uuid)
                if not ok:
                    (code, err_msg) = status
                    raise cfgm_common.exceptions.HttpError(code, err_msg)
    # end _validate_perms_in_request

    def _validate_resource_type(self, type):
        try:
            r_class = self.get_resource_class(type)
            return r_class.resource_type, r_class
        except TypeError:
            raise cfgm_common.exceptions.HttpError(
                404, "Resource type '%s' not found" % type)
    # end _validate_resource_type

    def _ensure_services_conn(
            self, api_name, obj_type, obj_uuid=None, obj_fq_name=None):
        # If not connected to zookeeper do not allow operations that
        # causes the state change
        if not self._db_conn._zk_db.is_connected():
            errmsg = 'No connection to zookeeper.'
            fq_name_str = ':'.join(obj_fq_name or [])
            self.config_object_error(
                obj_uuid, fq_name_str, obj_type, api_name, errmsg)
            raise cfgm_common.exceptions.HttpError(503, errmsg)

        # If there are too many pending updates to rabbit, do not allow
        # operations that cause state change
        npending = self._db_conn.dbe_oper_publish_pending()
        if (npending >= int(self._args.rabbit_max_pending_updates)):
            err_str = str(MaxRabbitPendingError(npending))
            raise cfgm_common.exceptions.HttpError(500, err_str)
    # end _ensure_services_conn

    def undo(self, result, obj_type, id=None, fq_name=None):
        (code, msg) = result

        if self._db_engine == 'cassandra':
            get_context().invoke_undo(code, msg, self.config_log)

        failed_stage = get_context().get_state()
        self.config_object_error(
            id, fq_name, obj_type, failed_stage, msg)
    # end undo

    # http_resource_<oper> - handlers invoked from
    # a. bottle route (on-the-wire) OR
    # b. internal requests
    # using normalized get_request() from ApiContext
    @log_api_stats
    def http_resource_create(self, obj_type):
        resource_type, r_class = self._validate_resource_type(obj_type)
        obj_dict = get_request().json[resource_type]

        # check visibility
        user_visible = (obj_dict.get('id_perms') or {}).get('user_visible', True)
        if not user_visible and not self.is_admin_request():
            result = 'This object is not visible by users'
            self.config_object_error(None, None, obj_type, 'http_post', result)
            raise cfgm_common.exceptions.HttpError(400, result)

        self._post_validate(obj_type, obj_dict=obj_dict)
        fq_name = obj_dict['fq_name']
        try:
            self._extension_mgrs['resourceApi'].map_method(
                 'pre_%s_create' %(obj_type), obj_dict)
        except RuntimeError:
            # lack of registered extension leads to RuntimeError
            pass
        except Exception as e:
            err_msg = 'In pre_%s_create an extension had error for %s' \
                      %(obj_type, obj_dict)
            err_msg += cfgm_common.utils.detailed_traceback()
            self.config_log(err_msg, level=SandeshLevel.SYS_NOTICE)

        # properties validator
        ok, result = self._validate_props_in_request(r_class, obj_dict)
        if not ok:
            result = 'Bad property in create: ' + result
            raise cfgm_common.exceptions.HttpError(400, result)

        # references validator
        ok, result = self._validate_refs_in_request(r_class, obj_dict)
        if not ok:
            result = 'Bad reference in create: ' + result
            raise cfgm_common.exceptions.HttpError(400, result)

        # common handling for all resource create
        (ok, result) = self._post_common(obj_type, obj_dict)
        if not ok:
            (code, msg) = result
            fq_name_str = ':'.join(obj_dict.get('fq_name', []))
            self.config_object_error(None, fq_name_str, obj_type, 'http_post', msg)
            raise cfgm_common.exceptions.HttpError(code, msg)

        uuid_in_req = result
        name = obj_dict['fq_name'][-1]
        fq_name = obj_dict['fq_name']

        db_conn = self._db_conn

        # if client gave parent_type of config-root, ignore and remove
        if 'parent_type' in obj_dict and obj_dict['parent_type'] == 'config-root':
            del obj_dict['parent_type']

        parent_class = None
        if 'parent_type' in obj_dict:
            # non config-root child, verify parent exists
            parent_res_type, parent_class = self._validate_resource_type(
                 obj_dict['parent_type'])
            parent_obj_type = parent_class.object_type
            parent_res_type = parent_class.resource_type
            parent_fq_name = obj_dict['fq_name'][:-1]
            try:
                parent_uuid = self._db_conn.fq_name_to_uuid(parent_obj_type,
                                                            parent_fq_name)
                (ok, status) = self._permissions.check_perms_write(
                    get_request(), parent_uuid)
                if not ok:
                    (code, err_msg) = status
                    raise cfgm_common.exceptions.HttpError(code, err_msg)
                self._permissions.set_user_role(get_request(), obj_dict)
                obj_dict['parent_uuid'] = parent_uuid
            except NoIdError:
                err_msg = 'Parent %s type %s does not exist' % (
                    pformat(parent_fq_name), parent_res_type)
                fq_name_str = ':'.join(parent_fq_name)
                self.config_object_error(None, fq_name_str, obj_type, 'http_post', err_msg)
                raise cfgm_common.exceptions.HttpError(400, err_msg)

        # Validate perms on references
        try:
            self._validate_perms_in_request(r_class, obj_type, obj_dict)
        except NoIdError:
            raise cfgm_common.exceptions.HttpError(
                400, 'Unknown reference in resource create %s.' %(obj_dict))

        # State modification starts from here. Ensure that cleanup is done for all state changes
        cleanup_on_failure = []

        def stateful_create():
            # Alloc and Store id-mappings before creating entry on pubsub store.
            # Else a subscriber can ask for an id mapping before we have stored it
            (ok, result) = db_conn.dbe_alloc(obj_type, obj_dict,
                                             uuid_in_req)
            if not ok:
                return (ok, result)
            get_context().push_undo(db_conn.dbe_release, obj_type, fq_name)

            obj_id = result
            env = get_request().headers.environ
            tenant_name = env.get(hdr_server_tenant()) or 'default-project'

            get_context().set_state('PRE_DBE_CREATE')
            # type-specific hook
            (ok, result) = r_class.pre_dbe_create(tenant_name, obj_dict,
                                                  db_conn)
            if not ok:
                return (ok, result)

            callable = getattr(r_class, 'http_post_collection_fail', None)
            if callable:
                cleanup_on_failure.append((callable, [tenant_name, obj_dict, db_conn]))

            ok, quota_limit, proj_uuid = r_class.get_quota_for_resource(obj_type,
                                                                        obj_dict, db_conn)
            if not ok:
                return ok, quota_limit

            get_context().set_state('DBE_CREATE')

            if quota_limit >= 0:
                #master_election
                ret = {'ok': None, 'result': None}
                def _create():
                    (ok, result) = r_class.check_for_quota(obj_type, obj_dict,
                                                           quota_limit, proj_uuid, db_conn)
                    if not ok:
                        ret['ok'] = ok
                        ret['result'] = result
                        return
                    (_ok, _result) = db_conn.dbe_create(obj_type, obj_id,
                                                        obj_dict)
                    ret['ok'] = _ok
                    ret['result'] = _result

                self._db_conn._zk_db.master_election("/vnc_api_server_obj_create/" + obj_type,
                                                     _create)
                if not ret['ok']:
                    return ret['ok'], ret['result']
            else:
                #normal execution
                (ok, result) = db_conn.dbe_create(obj_type, obj_id, obj_dict)
                if not ok:
                    return (ok, result)

            get_context().set_state('POST_DBE_CREATE')
            # type-specific hook
            try:
                ok, err_msg = r_class.post_dbe_create(tenant_name, obj_dict, db_conn)
            except Exception as e:
                ok = False
                err_msg = '%s:%s post_dbe_create had an exception: %s' %(
                    obj_type, obj_id, str(e))
                err_msg += cfgm_common.utils.detailed_traceback()

            if not ok:
                # Create is done, log to system, no point in informing user
                self.config_log(err_msg, level=SandeshLevel.SYS_ERR)

            return True, obj_id
        # end stateful_create

        try:
            ok, result = stateful_create()
        except Exception as e:
            ok = False
            err_msg = cfgm_common.utils.detailed_traceback()
            result = (500, err_msg)
        if not ok:
            fq_name_str = ':'.join(fq_name)
            self.undo(result, obj_type, fq_name=fq_name_str)
            code, msg = result
            raise cfgm_common.exceptions.HttpError(code, msg)

        rsp_body = {}
        rsp_body['name'] = name
        rsp_body['fq_name'] = fq_name
        rsp_body['uuid'] = result
        rsp_body['href'] = self.generate_url(resource_type, result)
        if parent_class:
            # non config-root child, send back parent uuid/href
            rsp_body['parent_uuid'] = parent_uuid
            rsp_body['parent_href'] = self.generate_url(parent_res_type,
                                                        parent_uuid)

        try:
            self._extension_mgrs['resourceApi'].map_method(
                'post_%s_create' %(obj_type), obj_dict)
        except RuntimeError:
            # lack of registered extension leads to RuntimeError
            pass
        except Exception as e:
            err_msg = 'In post_%s_create an extension had error for %s' \
                      %(obj_type, obj_dict)
            err_msg += cfgm_common.utils.detailed_traceback()
            self.config_log(err_msg, level=SandeshLevel.SYS_NOTICE)

        return {resource_type: rsp_body}
    # end http_resource_create

    @log_api_stats
    def http_resource_read(self, obj_type, id):
        resource_type, r_class = self._validate_resource_type(obj_type)
        try:
            self._extension_mgrs['resourceApi'].map_method(
                'pre_%s_read' %(obj_type), id)
        except Exception as e:
            pass

        etag = get_request().headers.get('If-None-Match')
        db_conn = self._db_conn
        try:
            req_obj_type = db_conn.uuid_to_obj_type(id)
            if req_obj_type != obj_type:
                raise cfgm_common.exceptions.HttpError(
                    404, 'No %s object found for id %s' %(resource_type, id))
            fq_name = db_conn.uuid_to_fq_name(id)
        except NoIdError as e:
            raise cfgm_common.exceptions.HttpError(404, str(e))

        # common handling for all resource get
        (ok, result) = self._get_common(get_request(), id)
        if not ok:
            (code, msg) = result
            self.config_object_error(
                id, None, obj_type, 'http_get', msg)
            raise cfgm_common.exceptions.HttpError(code, msg)

        db_conn = self._db_conn
        if etag:
            (ok, result) = db_conn.dbe_is_latest(id, etag.strip('"'))
            if not ok:
                # Not present in DB
                self.config_object_error(
                    id, None, obj_type, 'http_get', result)
                raise cfgm_common.exceptions.HttpError(404, result)

            is_latest = result
            if is_latest:
                # send Not-Modified, caches use this for read optimization
                bottle.response.status = 304
                return
        # end if etag

        # Generate field list for db layer
        obj_fields = r_class.prop_fields | r_class.ref_fields
        if 'fields' in get_request().query:
            obj_fields |= set(get_request().query.fields.split(','))
        else: # default props + children + refs + backrefs
            if 'exclude_back_refs' not in get_request().query:
                obj_fields |= r_class.backref_fields
            if 'exclude_children' not in get_request().query:
                obj_fields |= r_class.children_fields

        (ok, result) = r_class.pre_dbe_read(id, db_conn)
        if not ok:
            (code, msg) = result
            raise cfgm_common.exceptions.HttpError(code, msg)

        try:
            (ok, result) = db_conn.dbe_read(obj_type, id,
                list(obj_fields), ret_readonly=True)
            if not ok:
                self.config_object_error(id, None, obj_type, 'http_get', result)
        except NoIdError as e:
            # Not present in DB
            raise cfgm_common.exceptions.HttpError(404, str(e))
        if not ok:
            raise cfgm_common.exceptions.HttpError(500, result)

        # check visibility
        if (not result['id_perms'].get('user_visible', True) and
            not self.is_admin_request()):
            result = 'This object is not visible by users: %s' % id
            self.config_object_error(id, None, obj_type, 'http_get', result)
            raise cfgm_common.exceptions.HttpError(404, result)

        if not self.is_admin_request():
            result = self.obj_view(resource_type, result)

        (ok, err_msg) = r_class.post_dbe_read(result, db_conn)
        if not ok:
            (code, msg) = err_msg
            raise cfgm_common.exceptions.HttpError(code, msg)

        rsp_body = {}
        rsp_body['uuid'] = id
        rsp_body['name'] = result['fq_name'][-1]
        if 'exclude_hrefs' not in get_request().query:
            result = self.generate_hrefs(resource_type, result)
        rsp_body.update(result)
        id_perms = result['id_perms']
        bottle.response.set_header('ETag', '"' + id_perms['last_modified'] + '"')
        try:
            self._extension_mgrs['resourceApi'].map_method(
                'post_%s_read' %(obj_type), id, rsp_body)
        except Exception as e:
            pass

        return {resource_type: rsp_body}
    # end http_resource_read

    # filter object references based on permissions
    def obj_view(self, resource_type, obj_dict):
        ret_obj_dict = {}
        ret_obj_dict.update(obj_dict)
        r_class = self.get_resource_class(resource_type)
        obj_links = r_class.obj_links & set(obj_dict.keys())
        obj_uuids = [ref['uuid'] for link in obj_links for ref in list(obj_dict[link])]
        obj_dicts = self._db_conn._object_db.object_raw_read(obj_uuids, ["perms2"])
        uuid_to_perms2 = dict((o['uuid'], o['perms2']) for o in obj_dicts)

        for link_field in obj_links:
            links = obj_dict[link_field]

            # build new links in returned dict based on permissions on linked object
            ret_obj_dict[link_field] = [l for l in links
                if self._permissions.check_perms_read(get_request(), l['uuid'], id_perms=uuid_to_perms2[l['uuid']])[0] == True]

        return ret_obj_dict
    # end obj_view


    @log_api_stats
    def http_resource_update(self, obj_type, id):
        resource_type, r_class = self._validate_resource_type(obj_type)

        # Early return if there is no body or an empty body
        request = get_request()
        if (not hasattr(request, 'json') or
            not request.json or
            not request.json[resource_type]):
            return

        obj_dict = get_request().json[resource_type]

        try:
            obj_fields = r_class.prop_fields | r_class.ref_fields
            (read_ok, read_result) = self._db_conn.dbe_read(obj_type, id, obj_fields)
            if not read_ok:
                bottle.abort(
                    404, 'No %s object found for id %s' %(resource_type, id))
        except NoIdError as e:
            raise cfgm_common.exceptions.HttpError(404, str(e))

        self._put_common(
            'http_put', obj_type, id, read_result, req_obj_dict=obj_dict)

        rsp_body = {}
        rsp_body['uuid'] = id
        rsp_body['href'] = self.generate_url(resource_type, id)

        return {resource_type: rsp_body}
    # end http_resource_update

    @log_api_stats
    def http_resource_delete(self, obj_type, id):
        resource_type, r_class = self._validate_resource_type(obj_type)

        db_conn = self._db_conn
        # if obj doesn't exist return early
        try:
            req_obj_type = db_conn.uuid_to_obj_type(id)
            if req_obj_type != obj_type:
                raise cfgm_common.exceptions.HttpError(
                    404, 'No %s object found for id %s' %(resource_type, id))
            _ = db_conn.uuid_to_fq_name(id)
        except NoIdError:
            raise cfgm_common.exceptions.HttpError(
                404, 'ID %s does not exist' %(id))

        try:
            self._extension_mgrs['resourceApi'].map_method(
                'pre_%s_delete' %(obj_type), id)
        except RuntimeError:
            # lack of registered extension leads to RuntimeError
            pass
        except Exception as e:
            err_msg = 'In pre_%s_delete an extension had error for %s' \
                      %(obj_type, id)
            err_msg += cfgm_common.utils.detailed_traceback()
            self.config_log(err_msg, level=SandeshLevel.SYS_NOTICE)

        # read in obj from db (accepting error) to get details of it
        try:
            (read_ok, read_result) = db_conn.dbe_read(obj_type, id)
        except NoIdError as e:
            raise cfgm_common.exceptions.HttpError(404, str(e))
        if not read_ok:
            self.config_object_error(
                id, None, obj_type, 'http_delete', read_result)
            # proceed down to delete the resource

        # check visibility
        if (not read_result['id_perms'].get('user_visible', True) and
            not self.is_admin_request()):
            result = 'This object is not visible by users: %s' % id
            self.config_object_error(id, None, obj_type, 'http_delete', result)
            raise cfgm_common.exceptions.HttpError(404, result)

        # common handling for all resource delete
        parent_uuid = read_result.get('parent_uuid')
        (ok, del_result) = self._delete_common(
            get_request(), obj_type, id, parent_uuid)
        if not ok:
            (code, msg) = del_result
            self.config_object_error(id, None, obj_type, 'http_delete', msg)
            raise cfgm_common.exceptions.HttpError(code, msg)

        fq_name = read_result['fq_name']

        # fail if non-default children or non-derived backrefs exist
        default_names = {}
        for child_field in r_class.children_fields:
            child_type, is_derived = r_class.children_field_types[child_field]
            if is_derived:
                continue
            child_cls = self.get_resource_class(child_type)
            default_child_name = 'default-%s' %(
                child_cls(parent_type=obj_type).get_type())
            default_names[child_type] = default_child_name
            exist_hrefs = []
            for child in read_result.get(child_field, []):
                if child['to'][-1] == default_child_name:
                    continue
                exist_hrefs.append(
                    self.generate_url(child_type, child['uuid']))
            if exist_hrefs:
                err_msg = 'Delete when children still present: %s' %(
                    exist_hrefs)
                self.config_object_error(
                    id, None, obj_type, 'http_delete', err_msg)
                raise cfgm_common.exceptions.HttpError(409, err_msg)

        relaxed_refs = set(db_conn.dbe_get_relaxed_refs(id))
        for backref_field in r_class.backref_fields:
            backref_type, _, is_derived = \
                r_class.backref_field_types[backref_field]
            if is_derived:
                continue
            exist_hrefs = [self.generate_url(backref_type, backref['uuid'])
                           for backref in read_result.get(backref_field, [])
                               if backref['uuid'] not in relaxed_refs]
            if exist_hrefs:
                err_msg = 'Delete when resource still referred: %s' %(
                    exist_hrefs)
                self.config_object_error(
                    id, None, obj_type, 'http_delete', err_msg)
                raise cfgm_common.exceptions.HttpError(409, err_msg)

        # State modification starts from here. Ensure that cleanup is done for all state changes
        cleanup_on_failure = []

        def stateful_delete():
            get_context().set_state('PRE_DBE_DELETE')
            (ok, del_result) = r_class.pre_dbe_delete(id, read_result, db_conn)
            if not ok:
                return (ok, del_result)
            # Delete default children first
            for child_field in r_class.children_fields:
                child_type, is_derived = r_class.children_field_types[child_field]
                if is_derived:
                    continue
                if child_field in self._GENERATE_DEFAULT_INSTANCE:
                    self.delete_default_children(child_type, read_result)

            callable = getattr(r_class, 'http_delete_fail', None)
            if callable:
                cleanup_on_failure.append((callable, [id, read_result, db_conn]))

            get_context().set_state('DBE_DELETE')
            (ok, del_result) = db_conn.dbe_delete(obj_type, id, read_result)
            if not ok:
                return (ok, del_result)

            # type-specific hook
            get_context().set_state('POST_DBE_DELETE')
            try:
                ok, err_msg = r_class.post_dbe_delete(id, read_result, db_conn)
            except Exception as e:
                ok = False
                err_msg = '%s:%s post_dbe_delete had an exception: ' \
                          %(obj_type, id)
                err_msg += cfgm_common.utils.detailed_traceback()

            if not ok:
                # Delete is done, log to system, no point in informing user
                self.config_log(err_msg, level=SandeshLevel.SYS_ERR)

            return (True, '')
        # end stateful_delete

        try:
            ok, result = stateful_delete()
        except NoIdError as e:
            raise cfgm_common.exceptions.HttpError(
                404, 'No %s object found for id %s' %(resource_type, id))
        except Exception as e:
            ok = False
            err_msg = cfgm_common.utils.detailed_traceback()
            result = (500, err_msg)
        if not ok:
            self.undo(result, obj_type, id=id)
            code, msg = result
            raise cfgm_common.exceptions.HttpError(code, msg)

        try:
            self._extension_mgrs['resourceApi'].map_method(
                'post_%s_delete' %(obj_type), id, read_result)
        except RuntimeError:
            # lack of registered extension leads to RuntimeError
            pass
        except Exception as e:
            err_msg = 'In pre_%s_delete an extension had error for %s' \
                      %(obj_type, id)
            err_msg += cfgm_common.utils.detailed_traceback()
            self.config_log(err_msg, level=SandeshLevel.SYS_NOTICE)
    # end http_resource_delete

    @log_api_stats
    def http_resource_list(self, obj_type):
        resource_type, r_class = self._validate_resource_type(obj_type)

        db_conn = self._db_conn
        env = get_request().headers.environ
        parent_uuids = None
        back_ref_uuids = None
        obj_uuids = None
        if (('parent_fq_name_str' in get_request().query) and
            ('parent_type' in get_request().query)):
            parent_fq_name = get_request().query.parent_fq_name_str.split(':')
            parent_res_type = get_request().query.parent_type
            _, parent_class = self._validate_resource_type(parent_res_type)
            parent_uuids = [self._db_conn.fq_name_to_uuid(
                    parent_class.object_type, parent_fq_name)]
        elif 'parent_id' in get_request().query:
            parent_uuids = get_request().query.parent_id.split(',')
        if 'back_ref_id' in get_request().query:
            back_ref_uuids = get_request().query.back_ref_id.split(',')
        if 'obj_uuids' in get_request().query:
            obj_uuids = get_request().query.obj_uuids.split(',')

        # common handling for all resource get
        for parent_uuid in list(parent_uuids or []):
            (ok, result) = self._get_common(get_request(), parent_uuid)
            if not ok:
                parent_uuids.remove(parent_uuid)

        if obj_uuids is None and back_ref_uuids is None and parent_uuids == []:
            return {'%ss' %(resource_type): []}

        if 'count' in get_request().query:
            is_count = 'true' in get_request().query.count.lower()
        else:
            is_count = False

        if 'detail' in get_request().query:
            is_detail = 'true' in get_request().query.detail.lower()
        else:
            is_detail = False

        if 'fields' in get_request().query:
            req_fields = get_request().query.fields.split(',')
        else:
            req_fields = []

        if 'shared' in get_request().query:
            include_shared = 'true' in get_request().query.shared.lower()
        else:
            include_shared = False

        try:
            filters = utils.get_filters(get_request().query.filters)
        except Exception as e:
            raise cfgm_common.exceptions.HttpError(
                400, 'Invalid filter ' + get_request().query.filters)

        if 'exclude_hrefs' in get_request().query:
            exclude_hrefs = True
        else:
            exclude_hrefs = False

        return self._list_collection(obj_type, parent_uuids, back_ref_uuids,
                                     obj_uuids, is_count, is_detail, filters,
                                     req_fields, include_shared, exclude_hrefs)
    # end http_resource_list

    # internal_request_<oper> - handlers of internally generated requests
    # that save-ctx, generate-ctx and restore-ctx
    def internal_request_create(self, resource_type, obj_json):
        object_type = self.get_resource_class(resource_type).object_type
        try:
            orig_context = get_context()
            orig_request = get_request()
            b_req = bottle.BaseRequest(
                {'PATH_INFO': '/%ss' %(resource_type),
                 'bottle.app': orig_request.environ['bottle.app'],
                 'HTTP_X_USER': 'contrail-api',
                 'HTTP_X_ROLE': self.cloud_admin_role})
            json_as_dict = {'%s' %(resource_type): obj_json}
            i_req = context.ApiInternalRequest(
                b_req.url, b_req.urlparts, b_req.environ, b_req.headers,
                json_as_dict, None)
            set_context(context.ApiContext(internal_req=i_req))
            self.http_resource_create(object_type)
            return True, ""
        finally:
            set_context(orig_context)
    # end internal_request_create

    def internal_request_update(self, resource_type, obj_uuid, obj_json):
        object_type = self.get_resource_class(resource_type).object_type
        try:
            orig_context = get_context()
            orig_request = get_request()
            b_req = bottle.BaseRequest(
                {'PATH_INFO': '/%ss' %(resource_type),
                 'bottle.app': orig_request.environ['bottle.app'],
                 'HTTP_X_USER': 'contrail-api',
                 'HTTP_X_ROLE': self.cloud_admin_role})
            json_as_dict = {'%s' %(resource_type): obj_json}
            i_req = context.ApiInternalRequest(
                b_req.url, b_req.urlparts, b_req.environ, b_req.headers,
                json_as_dict, None)
            set_context(context.ApiContext(internal_req=i_req))
            self.http_resource_update(object_type, obj_uuid)
            return True, ""
        finally:
            set_context(orig_context)
    # end internal_request_update

    def internal_request_delete(self, resource_type, obj_uuid):
        object_type = self.get_resource_class(resource_type).object_type
        try:
            orig_context = get_context()
            orig_request = get_request()
            b_req = bottle.BaseRequest(
                {'PATH_INFO': '/%s/%s' %(resource_type, obj_uuid),
                 'bottle.app': orig_request.environ['bottle.app'],
                 'HTTP_X_USER': 'contrail-api',
                 'HTTP_X_ROLE': self.cloud_admin_role})
            i_req = context.ApiInternalRequest(
                b_req.url, b_req.urlparts, b_req.environ, b_req.headers,
                None, None)
            set_context(context.ApiContext(internal_req=i_req))
            self.http_resource_delete(object_type, obj_uuid)
            return True, ""
        finally:
            set_context(orig_context)
    # end internal_request_delete

    def internal_request_ref_update(self,
        res_type, obj_uuid, operation, ref_res_type, ref_uuid, attr=None):
        req_dict = {'type': res_type,
                    'uuid': obj_uuid,
                    'operation': operation,
                    'ref-type': ref_res_type,
                    'ref-uuid': ref_uuid,
                    'attr': attr}
        try:
            orig_context = get_context()
            orig_request = get_request()
            b_req = bottle.BaseRequest(
                {'PATH_INFO': '/ref-update',
                 'bottle.app': orig_request.environ['bottle.app'],
                 'HTTP_X_USER': 'contrail-api',
                 'HTTP_X_ROLE': self.cloud_admin_role})
            i_req = context.ApiInternalRequest(
                b_req.url, b_req.urlparts, b_req.environ, b_req.headers,
                req_dict, None)
            set_context(context.ApiContext(internal_req=i_req))
            self.ref_update_http_post()
            return True, ""
        finally:
            set_context(orig_context)
    # end internal_request_ref_update

    def alloc_vn_id(self, name):
        return self._db_conn._zk_db.alloc_vn_id(name) + 1

    def create_default_children(self, object_type, parent_obj):
        r_class = self.get_resource_class(object_type)
        for child_fields in r_class.children_fields:
            # Create a default child only if provisioned for
            child_res_type, is_derived =\
                r_class.children_field_types[child_fields]
            if is_derived:
                continue
            if child_res_type not in self._GENERATE_DEFAULT_INSTANCE:
                continue
            child_cls = self.get_resource_class(child_res_type)
            child_obj_type = child_cls.object_type
            child_obj = child_cls(parent_obj=parent_obj)
            child_dict = child_obj.__dict__
            child_dict['id_perms'] = self._get_default_id_perms()
            child_dict['perms2'] = self._get_default_perms2()
            (ok, result) = self._db_conn.dbe_alloc(child_obj_type, child_dict)
            if not ok:
                return (ok, result)
            obj_id = result

            # For virtual networks, allocate an ID
            if child_obj_type == 'virtual_network':
                child_dict['virtual_network_network_id'] = self.alloc_vn_id(
                    child_obj.get_fq_name_str())

            (ok, result) = self._db_conn.dbe_create(child_obj_type, obj_id,
                                                    child_dict)
            if not ok:
                # DB Create failed, log and stop further child creation.
                err_msg = "DB Create failed creating %s" % child_res_type
                self.config_log(err_msg, level=SandeshLevel.SYS_ERR)
                return (ok, result)

            # recurse down type hierarchy
            self.create_default_children(child_obj_type, child_obj)
    # end create_default_children

    def delete_default_children(self, resource_type, parent_dict):
        r_class = self.get_resource_class(resource_type)
        for child_field in r_class.children_fields:
            # Delete a default child only if provisioned for
            child_type, is_derived = r_class.children_field_types[child_field]
            if child_type not in self._GENERATE_DEFAULT_INSTANCE:
               continue
            child_cls = self.get_resource_class(child_type)
            # first locate default child then delete it")
            default_child_name = 'default-%s' %(child_type)
            child_infos = parent_dict.get(child_field, [])
            for child_info in child_infos:
                if child_info['to'][-1] == default_child_name:
                    default_child_id = child_info['uuid']
                    self.http_resource_delete(child_type, default_child_id)
                    break
    # end delete_default_children

    @classmethod
    def _generate_resource_crud_methods(cls, obj):
        for object_type, _ in all_resource_type_tuples:
            create_method = functools.partial(obj.http_resource_create,
                                              object_type)
            functools.update_wrapper(create_method, obj.http_resource_create)
            setattr(obj, '%ss_http_post' %(object_type), create_method)

            read_method = functools.partial(obj.http_resource_read,
                                            object_type)
            functools.update_wrapper(read_method, obj.http_resource_read)
            setattr(obj, '%s_http_get' %(object_type), read_method)

            update_method = functools.partial(obj.http_resource_update,
                                              object_type)
            functools.update_wrapper(update_method, obj.http_resource_update)
            setattr(obj, '%s_http_put' %(object_type), update_method)

            delete_method = functools.partial(obj.http_resource_delete,
                                              object_type)
            functools.update_wrapper(delete_method, obj.http_resource_delete)
            setattr(obj, '%s_http_delete' %(object_type), delete_method)

            list_method = functools.partial(obj.http_resource_list,
                                            object_type)
            functools.update_wrapper(list_method, obj.http_resource_list)
            setattr(obj, '%ss_http_get' %(object_type), list_method)
    # end _generate_resource_crud_methods

    @classmethod
    def _generate_resource_crud_uri(cls, obj):
        for object_type, resource_type in all_resource_type_tuples:
            # CRUD + list URIs of the form
            # obj.route('/virtual-network/<id>', 'GET', obj.virtual_network_http_get)
            # obj.route('/virtual-network/<id>', 'PUT', obj.virtual_network_http_put)
            # obj.route('/virtual-network/<id>', 'DELETE', obj.virtual_network_http_delete)
            # obj.route('/virtual-networks', 'POST', obj.virtual_networks_http_post)
            # obj.route('/virtual-networks', 'GET', obj.virtual_networks_http_get)

            # leaf resource
            obj.route('/%s/<id>' %(resource_type),
                      'GET',
                      getattr(obj, '%s_http_get' %(object_type)))
            obj.route('/%s/<id>' %(resource_type),
                      'PUT',
                      getattr(obj, '%s_http_put' %(object_type)))
            obj.route('/%s/<id>' %(resource_type),
                      'DELETE',
                      getattr(obj, '%s_http_delete' %(object_type)))
            # collection of leaf
            obj.route('/%ss' %(resource_type),
                      'POST',
                      getattr(obj, '%ss_http_post' %(object_type)))
            obj.route('/%ss' %(resource_type),
                      'GET',
                      getattr(obj, '%ss_http_get' %(object_type)))
    # end _generate_resource_crud_uri

    def __init__(self, args_str=None):
        self._db_conn = None
        self._resource_classes = {}
        self._args = None
        if not args_str:
            args_str = ' '.join(sys.argv[1:])
        self._parse_args(args_str)

        # aaa-mode is ignored if multi_tenancy is configured by user
        if self._args.multi_tenancy is None:
            # MT unconfigured by user - determine from aaa-mode
            if self.aaa_mode not in cfgm_common.AAA_MODE_VALID_VALUES:
                self.aaa_mode = cfgm_common.AAA_MODE_DEFAULT_VALUE
            self._args.multi_tenancy = self.aaa_mode != 'no-auth'
        else:
            # MT configured by user - ignore aaa-mode
            self.aaa_mode = "cloud-admin" if self._args.multi_tenancy else "no-auth"

        # set python logging level from logging_level cmdline arg
        if not self._args.logging_conf:
            logging.basicConfig(level = getattr(logging, self._args.logging_level))

        self._base_url = "http://%s:%s" % (self._args.listen_ip_addr,
                                           self._args.listen_port)

        # Generate LinkObjects for all entities
        links = []
        # Link for root
        links.append(LinkObject('root', self._base_url , '/config-root',
                                'config-root'))

        for _, resource_type in all_resource_type_tuples:
            link = LinkObject('collection',
                              self._base_url , '/%ss' %(resource_type),
                              '%s' %(resource_type))
            links.append(link)

        for _, resource_type in all_resource_type_tuples:
            link = LinkObject('resource-base',
                              self._base_url , '/%s' %(resource_type),
                              '%s' %(resource_type))
            links.append(link)

        self._homepage_links = links

        self._pipe_start_app = None

        #GreenletProfiler.set_clock_type('wall')
        self._profile_info = None

        for act_res in _ACTION_RESOURCES:
            link = LinkObject('action', self._base_url, act_res['uri'],
                              act_res['link_name'], act_res['method'])
            self._homepage_links.append(link)

        # Register for VN delete request. Disallow delete of system default VN
        self.route('/virtual-network/<id>', 'DELETE', self.virtual_network_http_delete)

        self.route('/documentation/<filename:path>',
                     'GET', self.documentation_http_get)
        self._homepage_links.insert(
            0, LinkObject('documentation', self._base_url,
                          '/documentation/index.html',
                          'documentation', 'GET'))

        # APIs to reserve/free block of IP address from a VN/Subnet
        self.route('/virtual-network/<id>/ip-alloc',
                     'POST', self.vn_ip_alloc_http_post)
        self._homepage_links.append(
            LinkObject('action', self._base_url,
                       '/virtual-network/%s/ip-alloc',
                       'virtual-network-ip-alloc', 'POST'))

        self.route('/virtual-network/<id>/ip-free',
                     'POST', self.vn_ip_free_http_post)
        self._homepage_links.append(
            LinkObject('action', self._base_url,
                       '/virtual-network/%s/ip-free',
                       'virtual-network-ip-free', 'POST'))

        # APIs to find out number of ip instances from given VN subnet
        self.route('/virtual-network/<id>/subnet-ip-count',
                     'POST', self.vn_subnet_ip_count_http_post)
        self._homepage_links.append(
            LinkObject('action', self._base_url,
                       '/virtual-network/%s/subnet-ip-count',
                       'virtual-network-subnet-ip-count', 'POST'))

        # Enable/Disable multi tenancy
        self.route('/multi-tenancy', 'GET', self.mt_http_get)
        self.route('/multi-tenancy', 'PUT', self.mt_http_put)
        self.route('/aaa-mode',      'GET', self.aaa_mode_http_get)
        self.route('/aaa-mode',      'PUT', self.aaa_mode_http_put)

        # randomize the collector list
        self._random_collectors = self._args.collectors
        self._chksum = "";
        if self._args.collectors:
            self._chksum = hashlib.md5(''.join(self._args.collectors)).hexdigest()
            self._random_collectors = random.sample(self._args.collectors, \
                                                    len(self._args.collectors))

        # sandesh init
        self._sandesh = Sandesh()
        # Reset the sandesh send rate limit  value
        if self._args.sandesh_send_rate_limit is not None:
            SandeshSystem.set_sandesh_send_rate_limit(
                self._args.sandesh_send_rate_limit)
        module = Module.API_SERVER
        module_name = ModuleNames[Module.API_SERVER]
        node_type = Module2NodeType[module]
        node_type_name = NodeTypeNames[node_type]
        self.table = "ObjectConfigNode"
        if self._args.worker_id:
            instance_id = self._args.worker_id
        else:
            instance_id = INSTANCE_ID_DEFAULT
        hostname = socket.gethostname()
        self._sandesh.init_generator(module_name, hostname,
                                     node_type_name, instance_id,
                                     self._random_collectors,
                                     'vnc_api_server_context',
                                     int(self._args.http_server_port),
                                     ['cfgm_common', 'vnc_cfg_api_server.sandesh'],
                                     logger_class=self._args.logger_class,
                                     logger_config_file=self._args.logging_conf,
                                     config=self._args.sandesh_config)
        self._sandesh.trace_buffer_create(name="VncCfgTraceBuf", size=1000)
        self._sandesh.trace_buffer_create(name="RestApiTraceBuf", size=1000)
        self._sandesh.trace_buffer_create(name="DBRequestTraceBuf", size=1000)
        self._sandesh.trace_buffer_create(name="DBUVERequestTraceBuf", size=1000)
        self._sandesh.trace_buffer_create(name="MessageBusNotifyTraceBuf",
                                          size=1000)

        self._sandesh.set_logging_params(
            enable_local_log=self._args.log_local,
            category=self._args.log_category,
            level=self._args.log_level,
            file=self._args.log_file,
            enable_syslog=self._args.use_syslog,
            syslog_facility=self._args.syslog_facility)

        ConnectionState.init(self._sandesh, hostname, module_name,
                instance_id,
                staticmethod(ConnectionState.get_process_state_cb),
                NodeStatusUVE, NodeStatus, self.table)

        # Address Management interface
        addr_mgmt = vnc_addr_mgmt.AddrMgmt(self)
        self._addr_mgmt = addr_mgmt
        vnc_cfg_types.Resource.addr_mgmt = addr_mgmt

        # DB interface initialization
        if self._args.wipe_config:
            self._db_connect(True)
        else:
            self._db_connect(self._args.reset_config)
            self._db_init_entries()

        # API/Permissions check
        # after db init (uses db_conn)
        self._rbac = vnc_rbac.VncRbac(self, self._db_conn)
        self._permissions = vnc_perms.VncPermissions(self, self._args)
        if self.is_rbac_enabled():
            self._create_default_rbac_rule()
        if self.is_multi_tenancy_set():
            self._generate_obj_view_links()

        if os.path.exists('/usr/bin/contrail-version'):
            cfgm_cpu_uve = ModuleCpuState()
            cfgm_cpu_uve.name = socket.gethostname()
            cfgm_cpu_uve.config_node_ip = self.get_server_ip()

            command = "contrail-version contrail-config | grep 'contrail-config'"
            version = os.popen(command).read()
            _, rpm_version, build_num = version.split()
            cfgm_cpu_uve.build_info = build_info + '"build-id" : "' + \
                                      rpm_version + '", "build-number" : "' + \
                                      build_num + '"}]}'

            cpu_info_trace = ModuleCpuStateTrace(data=cfgm_cpu_uve, sandesh=self._sandesh)
            cpu_info_trace.send(sandesh=self._sandesh)

        self.re_uuid = re.compile('^[0-9A-F]{8}-?[0-9A-F]{4}-?4[0-9A-F]{3}-?[89AB][0-9A-F]{3}-?[0-9A-F]{12}$',
                                  re.IGNORECASE)

        # VncZkClient client assignment
        vnc_cfg_types.Resource.vnc_zk_client = self._db_conn._zk_db

        # Load extensions
        self._extension_mgrs = {}
        self._load_extensions()

        # Authn/z interface
        if self._args.auth == 'keystone':
            auth_svc = vnc_auth_keystone.AuthServiceKeystone(self, self._args)
        else:
            auth_svc = vnc_auth.AuthService(self, self._args)

        self._pipe_start_app = auth_svc.get_middleware_app()
        self._auth_svc = auth_svc

        if int(self._args.worker_id) == 0:
            try:
                self._extension_mgrs['resync'].map(
                    self._resync_domains_projects)
            except RuntimeError:
                # lack of registered extension leads to RuntimeError
                pass
            except Exception as e:
                err_msg = cfgm_common.utils.detailed_traceback()
                self.config_log(err_msg, level=SandeshLevel.SYS_ERR)

        # following allowed without authentication
        self.white_list = [
            '^/documentation',  # allow all documentation
            '^/$',              # allow discovery
        ]
    # end __init__

    def _extensions_transform_request(self, request):
        extensions = self._extension_mgrs.get('resourceApi')
        if not extensions or not extensions.names():
            return None
        return extensions.map_method(
                    'transform_request', request)
    # end _extensions_transform_request

    def _extensions_validate_request(self, request):
        extensions = self._extension_mgrs.get('resourceApi')
        if not extensions or not extensions.names():
            return None
        return extensions.map_method(
                    'validate_request', request)
    # end _extensions_validate_request

    def _extensions_transform_response(self, request, response):
        extensions = self._extension_mgrs.get('resourceApi')
        if not extensions or not extensions.names():
            return None
        return extensions.map_method(
                    'transform_response', request, response)
    # end _extensions_transform_response

    @ignore_exceptions
    def _generate_rest_api_request_trace(self):
        method = get_request().method.upper()
        if method == 'GET':
            return None

        req_id = get_request().headers.get('X-Request-Id',
                                            'req-%s' %(str(uuid.uuid4())))
        gevent.getcurrent().trace_request_id = req_id
        url = get_request().url
        if method == 'DELETE':
            req_data = ''
        else:
            try:
                req_data = json.dumps(get_request().json)
            except Exception as e:
                req_data = '%s: Invalid request body' %(e)
        rest_trace = RestApiTrace(request_id=req_id)
        rest_trace.url = url
        rest_trace.method = method
        rest_trace.request_data = req_data
        return rest_trace
    # end _generate_rest_api_request_trace

    @ignore_exceptions
    def _generate_rest_api_response_trace(self, rest_trace, response):
        if not rest_trace:
            return

        rest_trace.status = bottle.response.status
        rest_trace.response_body = json.dumps(response)
        rest_trace.trace_msg(name='RestApiTraceBuf', sandesh=self._sandesh)
    # end _generate_rest_api_response_trace

    # Public Methods
    def route(self, uri, method, handler):
        @use_context
        def handler_trap_exception(*args, **kwargs):
            try:
                trace = None
                self._extensions_transform_request(get_request())
                self._extensions_validate_request(get_request())

                trace = self._generate_rest_api_request_trace()
                (ok, status) = self._rbac.validate_request(get_request())
                if not ok:
                    (code, err_msg) = status
                    raise cfgm_common.exceptions.HttpError(code, err_msg)
                response = handler(*args, **kwargs)
                self._generate_rest_api_response_trace(trace, response)

                self._extensions_transform_response(get_request(), response)

                return response
            except Exception as e:
                if trace:
                    trace.trace_msg(name='RestApiTraceBuf',
                        sandesh=self._sandesh)
                # don't log details of cfgm_common.exceptions.HttpError i.e handled error cases
                if isinstance(e, cfgm_common.exceptions.HttpError):
                    bottle.abort(e.status_code, e.content)
                else:
                    string_buf = StringIO()
                    cgitb_hook(file=string_buf, format="text")
                    err_msg = string_buf.getvalue()
                    self.config_log(err_msg, level=SandeshLevel.SYS_ERR)
                    raise

        self.api_bottle.route(uri, method, handler_trap_exception)
    # end route

    def get_args(self):
        return self._args
    # end get_args

    def get_server_ip(self):
        ip_list = []
        for i in netifaces.interfaces():
            try:
                if netifaces.AF_INET in netifaces.ifaddresses(i):
                    addr = netifaces.ifaddresses(i)[netifaces.AF_INET][0][
                        'addr']
                    if addr != '127.0.0.1' and addr not in ip_list:
                        ip_list.append(addr)
            except ValueError, e:
                self.config_log("Skipping interface %s" % i,
                                level=SandeshLevel.SYS_DEBUG)
        return ip_list
    # end get_server_ip

    def get_listen_ip(self):
        return self._args.listen_ip_addr
    # end get_listen_ip

    def get_server_port(self):
        return self._args.listen_port
    # end get_server_port

    def get_worker_id(self):
        return int(self._args.worker_id)
    # end get_worker_id

    def get_pipe_start_app(self):
        return self._pipe_start_app
    # end get_pipe_start_app

    def get_rabbit_health_check_interval(self):
        return float(self._args.rabbit_health_check_interval)
    # end get_rabbit_health_check_interval

    def is_auth_disabled(self):
        return self._args.auth is None or self._args.auth.lower() != 'keystone'

    def is_admin_request(self):
        if not self.is_multi_tenancy_set():
            return True

        env = bottle.request.headers.environ
        for field in ('HTTP_X_API_ROLE', 'HTTP_X_ROLE'):
            if field in env:
                roles = env[field].split(',')
                return has_role(self.cloud_admin_role, roles)
        return False

    def get_auth_headers_from_token(self, request, token):
        if self.is_auth_disabled() or not self.is_multi_tenancy_set():
            return {}

        return self._auth_svc.get_auth_headers_from_token(request, token)
    # end get_auth_headers_from_token

    def _generate_obj_view_links(self):
        for object_type, resource_type in all_resource_type_tuples:
            r_class = self.get_resource_class(resource_type)
            r_class.obj_links = (r_class.ref_fields | r_class.backref_fields | r_class.children_fields)

    # Check for the system created VN. Disallow such VN delete
    def virtual_network_http_delete(self, id):
        db_conn = self._db_conn
        # if obj doesn't exist return early
        try:
            obj_type = db_conn.uuid_to_obj_type(id)
            if obj_type != 'virtual_network':
                raise cfgm_common.exceptions.HttpError(
                    404, 'No virtual-network object found for id %s' %(id))
            vn_name = db_conn.uuid_to_fq_name(id)
        except NoIdError:
            raise cfgm_common.exceptions.HttpError(
                404, 'ID %s does not exist' %(id))
        if (vn_name == cfgm_common.IP_FABRIC_VN_FQ_NAME or
            vn_name == cfgm_common.LINK_LOCAL_VN_FQ_NAME):
            raise cfgm_common.exceptions.HttpError(
                409,
                'Can not delete system created default virtual-network '+id)
        super(VncApiServer, self).virtual_network_http_delete(id)
   # end

    @use_context
    def homepage_http_get(self):
        json_body = {}
        json_links = []
        # strip trailing '/' in url
        url = get_request().url[:-1]
        for link in self._homepage_links:
            # strip trailing '/' in url
            json_links.append(
                {'link': link.to_dict(with_url=url)}
            )

        json_body = {"href": url, "links": json_links}

        return json_body
    # end homepage_http_get

    def documentation_http_get(self, filename):
        # ubuntu packaged path
        doc_root = '/usr/share/doc/contrail-config/doc/contrail-config/html/'
        if not os.path.exists(doc_root):
            # centos packaged path
            doc_root='/usr/share/doc/python-vnc_cfg_api_server/contrial-config/html/'

        return bottle.static_file(
                filename,
                root=doc_root)
    # end documentation_http_get

    def obj_perms_http_get(self):
        if self.is_auth_disabled() or not self.is_multi_tenancy_set():
            result = {
                'token_info': None,
                'is_cloud_admin_role': False,
                'is_global_read_only_role': False,
                'permissions': 'RWX'
            }
            return result

        if 'HTTP_X_USER_TOKEN' not in get_request().environ:
            raise cfgm_common.exceptions.HttpError(
                400, 'User token needed for validation')
        user_token = get_request().environ['HTTP_X_USER_TOKEN'].encode("ascii")
        obj_uuid = None
        if 'uuid' in get_request().query:
            obj_uuid = get_request().query.uuid

        # get permissions in internal context
        try:
            orig_context = get_context()
            orig_request = get_request()
            b_req = bottle.BaseRequest(
                {
                 'HTTP_X_AUTH_TOKEN':  user_token,
                 'REQUEST_METHOD'   : 'GET',
                 'bottle.app': orig_request.environ['bottle.app'],
                })
            i_req = context.ApiInternalRequest(
                b_req.url, b_req.urlparts, b_req.environ, b_req.headers, None, None)
            set_context(context.ApiContext(internal_req=i_req))
            token_info = self._auth_svc.validate_user_token(get_request())

            # roles in result['token_info']['access']['user']['roles']
            if token_info:
                result = {'token_info' : token_info}
                # Handle v2 and v3 responses
                roles_list = []
                if 'access' in token_info:
                    roles_list = [roles['name'] for roles in \
                        token_info['access']['user']['roles']]
                elif 'token' in token_info:
                    roles_list = [roles['name'] for roles in \
                        token_info['token']['roles']]
                result['is_cloud_admin_role'] = has_role(self.cloud_admin_role, roles_list)
                result['is_global_read_only_role'] = has_role(self.global_read_only_role, roles_list)
                if obj_uuid:
                    result['permissions'] = self._permissions.obj_perms(get_request(), obj_uuid)
            else:
                raise cfgm_common.exceptions.HttpError(403, " Permission denied")
        finally:
            set_context(orig_context)
        return result
    #end obj_perms_http_get

    def invalid_uuid(self, uuid):
        return self.re_uuid.match(uuid) == None
    def invalid_access(self, access):
        return type(access) is not int or access not in range(0,8)
    def invalid_share_type(self, share_type):
        return share_type not in cfgm_common.PERMS2_VALID_SHARE_TYPES

    # change ownership of an object
    def obj_chown_http_post(self):

        try:
            obj_uuid = get_request().json['uuid']
            owner = get_request().json['owner']
        except Exception as e:
            raise cfgm_common.exceptions.HttpError(400, str(e))
        if self.invalid_uuid(obj_uuid) or self.invalid_uuid(owner):
            raise cfgm_common.exceptions.HttpError(
                400, "Bad Request, invalid object or owner id")

        try:
            obj_type = self._db_conn.uuid_to_obj_type(obj_uuid)
        except NoIdError:
            raise cfgm_common.exceptions.HttpError(400, 'Invalid object id')

        self._ensure_services_conn('chown', obj_type, obj_uuid=obj_uuid)

        # ensure user has RW permissions to object
        perms = self._permissions.obj_perms(get_request(), obj_uuid)
        if not 'RW' in perms:
            raise cfgm_common.exceptions.HttpError(403, " Permission denied")

        (ok, obj_dict) = self._db_conn.dbe_read(obj_type, obj_uuid,
                                                obj_fields=['perms2'])
        obj_dict['perms2']['owner'] = owner
        self._db_conn.dbe_update(obj_type, obj_uuid, obj_dict)

        msg = "chown: %s owner set to %s" % (obj_uuid, owner)
        self.config_log(msg, level=SandeshLevel.SYS_NOTICE)

        return {}
    #end obj_chown_http_post

    # chmod for an object
    def obj_chmod_http_post(self):
        try:
            obj_uuid = get_request().json['uuid']
        except Exception as e:
            raise cfgm_common.exceptions.HttpError(400, str(e))
        if self.invalid_uuid(obj_uuid):
            raise cfgm_common.exceptions.HttpError(
                400, "Bad Request, invalid object id")

        try:
            obj_type = self._db_conn.uuid_to_obj_type(obj_uuid)
        except NoIdError:
            raise cfgm_common.exceptions.HttpError(400, 'Invalid object id')

        self._ensure_services_conn('chmod', obj_type, obj_uuid=obj_uuid)

        # ensure user has RW permissions to object
        perms = self._permissions.obj_perms(get_request(), obj_uuid)
        if not 'RW' in perms:
            raise cfgm_common.exceptions.HttpError(403, " Permission denied")

        request_params = get_request().json
        owner         = request_params.get('owner')
        share         = request_params.get('share')
        owner_access  = request_params.get('owner_access')
        global_access = request_params.get('global_access')

        (ok, obj_dict) = self._db_conn.dbe_read(obj_type, obj_uuid,
                             obj_fields=['perms2', 'is_shared'])
        obj_perms = obj_dict['perms2']
        old_perms = '%s/%d %d %s' % (obj_perms['owner'],
            obj_perms['owner_access'], obj_perms['global_access'],
            ['%s:%d' % (item['tenant'], item['tenant_access']) for item in obj_perms['share']])

        if owner:
            if self.invalid_uuid(owner):
                raise cfgm_common.exceptions.HttpError(
                    400, "Bad Request, invalid owner")
            obj_perms['owner'] = owner.replace('-','')
        if owner_access is not None:
            if self.invalid_access(owner_access):
                raise cfgm_common.exceptions.HttpError(
                    400, "Bad Request, invalid owner_access value")
            obj_perms['owner_access'] = owner_access
        if share is not None:
            try:
                for item in share:
                    """
                    item['tenant'] := [<share_type>:] <uuid>
                    share_type := ['domain' | 'tenant']
                    """
                    (share_type, share_id) = cfgm_common.utils.shareinfo_from_perms2_tenant(item['tenant'])
                    if self.invalid_share_type(share_type) or self.invalid_uuid(share_id) or self.invalid_access(item['tenant_access']):
                        raise cfgm_common.exceptions.HttpError(
                            400, "Bad Request, invalid share list")
            except Exception as e:
                raise cfgm_common.exceptions.HttpError(400, str(e))
            obj_perms['share'] = share
        if global_access is not None:
            if self.invalid_access(global_access):
                raise cfgm_common.exceptions.HttpError(
                    400, "Bad Request, invalid global_access value")
            obj_perms['global_access'] = global_access
            obj_dict['is_shared'] = (global_access != 0)

        new_perms = '%s/%d %d %s' % (obj_perms['owner'],
            obj_perms['owner_access'], obj_perms['global_access'],
            ['%s:%d' % (item['tenant'], item['tenant_access']) for item in obj_perms['share']])

        self._db_conn.dbe_update(obj_type, obj_uuid, obj_dict)
        msg = "chmod: %s perms old=%s, new=%s" % (obj_uuid, old_perms, new_perms)
        self.config_log(msg, level=SandeshLevel.SYS_NOTICE)

        return {}
    # end obj_chmod_http_post

    def prop_collection_http_get(self):
        if 'uuid' not in get_request().query:
            raise cfgm_common.exceptions.HttpError(
                400, 'Object uuid needed for property collection get')
        obj_uuid = get_request().query.uuid

        if 'fields' not in get_request().query:
            raise cfgm_common.exceptions.HttpError(
                400, 'Object fields needed for property collection get')
        obj_fields = get_request().query.fields.split(',')

        if 'position' in get_request().query:
            fields_position = get_request().query.position
        else:
            fields_position = None

        try:
            obj_type = self._db_conn.uuid_to_obj_type(obj_uuid)
        except NoIdError:
            raise cfgm_common.exceptions.HttpError(
                404, 'Object Not Found: ' + obj_uuid)
        resource_class = self.get_resource_class(obj_type)

        for obj_field in obj_fields:
            if ((obj_field not in resource_class.prop_list_fields) and
                (obj_field not in resource_class.prop_map_fields)):
                err_msg = '%s neither "ListProperty" nor "MapProperty"' %(
                    obj_field)
                raise cfgm_common.exceptions.HttpError(400, err_msg)
        # request validations over

        # common handling for all resource get
        (ok, result) = self._get_common(get_request(), obj_uuid)
        if not ok:
            (code, msg) = result
            self.config_object_error(
                obj_uuid, None, None, 'prop_collection_http_get', msg)
            raise cfgm_common.exceptions.HttpError(code, msg)

        try:
            ok, result = self._db_conn.prop_collection_get(
                obj_type, obj_uuid, obj_fields, fields_position)
            if not ok:
                self.config_object_error(
                    obj_uuid, None, None, 'prop_collection_http_get', result)
        except NoIdError as e:
            # Not present in DB
            raise cfgm_common.exceptions.HttpError(404, str(e))
        if not ok:
            raise cfgm_common.exceptions.HttpError(500, result)

        # check visibility
        if (not result['id_perms'].get('user_visible', True) and
            not self.is_admin_request()):
            result = 'This object is not visible by users: %s' % id
            self.config_object_error(
                id, None, None, 'prop_collection_http_get', result)
            raise cfgm_common.exceptions.HttpError(404, result)

        # Prepare response
        del result['id_perms']

        return result
    # end prop_collection_http_get

    def prop_collection_http_post(self):
        request_params = get_request().json
        # validate each requested operation
        obj_uuid = request_params.get('uuid')
        if not obj_uuid:
            err_msg = 'Error: prop_collection_update needs obj_uuid'
            raise cfgm_common.exceptions.HttpError(400, err_msg)

        try:
            obj_type = self._db_conn.uuid_to_obj_type(obj_uuid)
        except NoIdError:
            raise cfgm_common.exceptions.HttpError(
                404, 'Object Not Found: ' + obj_uuid)
        resource_class = self.get_resource_class(obj_type)

        for req_param in request_params.get('updates') or []:
            obj_field = req_param.get('field')
            if obj_field in resource_class.prop_list_fields:
                prop_coll_type = 'list'
            elif obj_field in resource_class.prop_map_fields:
                prop_coll_type = 'map'
            else:
                err_msg = '%s neither "ListProperty" nor "MapProperty"' %(
                    obj_field)
                raise cfgm_common.exceptions.HttpError(400, err_msg)

            req_oper = req_param.get('operation').lower()
            field_val = req_param.get('value')
            field_pos = str(req_param.get('position'))
            prop_type = resource_class.prop_field_types[obj_field]['xsd_type']
            prop_cls = cfgm_common.utils.str_to_class(prop_type, __name__)
            prop_val_type = prop_cls.attr_field_type_vals[prop_cls.attr_fields[0]]['attr_type']
            prop_val_cls = cfgm_common.utils.str_to_class(prop_val_type, __name__)
            try:
                self._validate_complex_type(prop_val_cls, field_val)
            except Exception as e:
                raise cfgm_common.exceptions.HttpError(400, str(e))
            if prop_coll_type == 'list':
                if req_oper not in ('add', 'modify', 'delete'):
                    err_msg = 'Unsupported operation %s in request %s' %(
                        req_oper, json.dumps(req_param))
                    raise cfgm_common.exceptions.HttpError(400, err_msg)
                if ((req_oper == 'add') and field_val is None):
                    err_msg = 'Add needs field value in request %s' %(
                        req_oper, json.dumps(req_param))
                    raise cfgm_common.exceptions.HttpError(400, err_msg)
                elif ((req_oper == 'modify') and
                    None in (field_val, field_pos)):
                    err_msg = 'Modify needs field value and position in request %s' %(
                        req_oper, json.dumps(req_param))
                    raise cfgm_common.exceptions.HttpError(400, err_msg)
                elif ((req_oper == 'delete') and field_pos is None):
                    err_msg = 'Delete needs field position in request %s' %(
                        req_oper, json.dumps(req_param))
                    raise cfgm_common.exceptions.HttpError(400, err_msg)
            elif prop_coll_type == 'map':
                if req_oper not in ('set', 'delete'):
                    err_msg = 'Unsupported operation %s in request %s' %(
                        req_oper, json.dumps(req_param))
                    raise cfgm_common.exceptions.HttpError(400, err_msg)
                if ((req_oper == 'set') and field_val is None):
                    err_msg = 'Set needs field value in request %s' %(
                        req_oper, json.dumps(req_param))
                elif ((req_oper == 'delete') and field_pos is None):
                    err_msg = 'Delete needs field position in request %s' %(
                        req_oper, json.dumps(req_param))
                    raise cfgm_common.exceptions.HttpError(400, err_msg)

        # Validations over. Invoke type specific hook and extension manager
        try:
            obj_fields = resource_class.prop_fields | resource_class.ref_fields
            (read_ok, read_result) = self._db_conn.dbe_read(obj_type, obj_uuid)
        except NoIdError:
            raise cfgm_common.exceptions.HttpError(
                404, 'Object Not Found: '+obj_uuid)
        except Exception as e:
            read_ok = False
            read_result = cfgm_common.utils.detailed_traceback()

        if not read_ok:
            self.config_object_error(
                obj_uuid, None, obj_type, 'prop_collection_update', read_result)
            raise cfgm_common.exceptions.HttpError(500, read_result)

        self._put_common(
            'prop-collection-update', obj_type, obj_uuid, read_result,
             req_prop_coll_updates=request_params.get('updates'))
    # end prop_collection_http_post

    def ref_update_http_post(self):
        # grab fields
        type = get_request().json.get('type')
        res_type, res_class = self._validate_resource_type(type)
        obj_uuid = get_request().json.get('uuid')
        ref_type = get_request().json.get('ref-type')
        ref_field = '%s_refs' %(ref_type.replace('-', '_'))
        ref_res_type, ref_class = self._validate_resource_type(ref_type)
        operation = get_request().json.get('operation')
        ref_uuid = get_request().json.get('ref-uuid')
        ref_fq_name = get_request().json.get('ref-fq-name')
        attr = get_request().json.get('attr')

        # validate fields
        if None in (res_type, obj_uuid, ref_res_type, operation):
            err_msg = 'Bad Request: type/uuid/ref-type/operation is null: '
            err_msg += '%s, %s, %s, %s.' \
                        %(res_type, obj_uuid, ref_res_type, operation)
            raise cfgm_common.exceptions.HttpError(400, err_msg)

        operation = operation.upper()
        if operation not in ['ADD', 'DELETE']:
            err_msg = 'Bad Request: operation should be add or delete: %s' \
                      %(operation)
            raise cfgm_common.exceptions.HttpError(400, err_msg)

        if not ref_uuid and not ref_fq_name:
            err_msg = 'Bad Request: ref-uuid or ref-fq-name must be specified'
            raise cfgm_common.exceptions.HttpError(400, err_msg)

        obj_type = res_class.object_type
        ref_obj_type = ref_class.object_type
        if not ref_uuid:
            try:
                ref_uuid = self._db_conn.fq_name_to_uuid(ref_obj_type, ref_fq_name)
            except NoIdError:
                raise cfgm_common.exceptions.HttpError(
                    404, 'Name ' + pformat(ref_fq_name) + ' not found')

        # To verify existence of the reference being added
        if operation == 'ADD':
            try:
                (read_ok, read_result) = self._db_conn.dbe_read(
                    ref_obj_type, ref_uuid, obj_fields=['fq_name'])
            except NoIdError:
                raise cfgm_common.exceptions.HttpError(
                    404, 'Object Not Found: ' + ref_uuid)
            except Exception as e:
                read_ok = False
                read_result = cfgm_common.utils.detailed_traceback()

        # To invoke type specific hook and extension manager
        try:
            obj_fields = res_class.prop_fields | res_class.ref_fields
            (read_ok, read_result) = self._db_conn.dbe_read(
                obj_type, obj_uuid, obj_fields)
        except NoIdError:
            raise cfgm_common.exceptions.HttpError(
                404, 'Object Not Found: '+obj_uuid)
        except Exception as e:
            read_ok = False
            read_result = cfgm_common.utils.detailed_traceback()

        if not read_ok:
            self.config_object_error(obj_uuid, None, obj_type, 'ref_update', read_result)
            raise cfgm_common.exceptions.HttpError(500, read_result)

        obj_dict = {'uuid': obj_uuid}
        if ref_field in read_result:
            obj_dict[ref_field] = copy.deepcopy(read_result[ref_field])

        if operation == 'ADD':
            if ref_obj_type+'_refs' not in obj_dict:
                obj_dict[ref_obj_type+'_refs'] = []
            existing_ref = [ref for ref in obj_dict[ref_obj_type+'_refs']
                            if ref['uuid'] == ref_uuid]
            if existing_ref:
                ref['attr'] = attr
            else:
                obj_dict[ref_obj_type+'_refs'].append(
                    {'to':ref_fq_name, 'uuid': ref_uuid, 'attr':attr})
        elif operation == 'DELETE':
            for old_ref in obj_dict.get(ref_obj_type+'_refs', []):
                if old_ref['to'] == ref_fq_name or old_ref['uuid'] == ref_uuid:
                    obj_dict[ref_obj_type+'_refs'].remove(old_ref)
                    break

        self._put_common(
            'ref-update', obj_type, obj_uuid, read_result,
             req_obj_dict=obj_dict)

        return {'uuid': obj_uuid}
    # end ref_update_http_post

    def ref_relax_for_delete_http_post(self):
        self._post_common(None, {})
        # grab fields
        obj_uuid = get_request().json.get('uuid')
        ref_uuid = get_request().json.get('ref-uuid')

        # validate fields
        if None in (obj_uuid, ref_uuid):
            err_msg = 'Bad Request: Both uuid and ref-uuid should be specified: '
            err_msg += '%s, %s.' %(obj_uuid, ref_uuid)
            raise cfgm_common.exceptions.HttpError(400, err_msg)

        try:
            obj_type = self._db_conn.uuid_to_obj_type(obj_uuid)
            self._db_conn.ref_relax_for_delete(obj_uuid, ref_uuid)
        except NoIdError:
            raise cfgm_common.exceptions.HttpError(
                404, 'uuid ' + obj_uuid + ' not found')

        apiConfig = VncApiCommon()
        apiConfig.object_type = obj_type
        fq_name = self._db_conn.uuid_to_fq_name(obj_uuid)
        apiConfig.identifier_name=':'.join(fq_name)
        apiConfig.identifier_uuid = obj_uuid
        apiConfig.operation = 'ref-relax-for-delete'
        try:
            body = json.dumps(get_request().json)
        except:
            body = str(get_request().json)
        apiConfig.body = body

        self._set_api_audit_info(apiConfig)
        log = VncApiConfigLog(api_log=apiConfig, sandesh=self._sandesh)
        log.send(sandesh=self._sandesh)

        return {'uuid': obj_uuid}
    # end ref_relax_for_delete_http_post

    def fq_name_to_id_http_post(self):
        self._post_common(None, {})
        type = get_request().json.get('type')
        res_type, r_class = self._validate_resource_type(type)
        obj_type = r_class.object_type
        fq_name = get_request().json['fq_name']

        try:
            id = self._db_conn.fq_name_to_uuid(obj_type, fq_name)
        except NoIdError:
            raise cfgm_common.exceptions.HttpError(
                404, 'Name ' + pformat(fq_name) + ' not found')

        # ensure user has access to this id
        ok, result = self._permissions.check_perms_read(bottle.request, id)
        if not ok:
            err_code, err_msg = result
            raise cfgm_common.exceptions.HttpError(err_code, err_msg)

        return {'uuid': id}
    # end fq_name_to_id_http_post

    def id_to_fq_name_http_post(self):
        self._post_common(None, {})
        obj_uuid = get_request().json['uuid']

        # ensure user has access to this id
        ok, result = self._permissions.check_perms_read(get_request(), obj_uuid)
        if not ok:
            err_code, err_msg = result
            raise cfgm_common.exceptions.HttpError(err_code, err_msg)

        try:
            fq_name = self._db_conn.uuid_to_fq_name(obj_uuid)
        except NoIdError:
            raise cfgm_common.exceptions.HttpError(
               404, 'UUID ' + obj_uuid + ' not found')

        obj_type = self._db_conn.uuid_to_obj_type(obj_uuid)
        res_type = self.get_resource_class(obj_type).resource_type
        return {'fq_name': fq_name, 'type': res_type}
    # end id_to_fq_name_http_post

    # Enables a user-agent to store and retrieve key-val pair
    # TODO this should be done only for special/quantum plugin
    def useragent_kv_http_post(self):
        self._post_common(None, {})

        request_params = get_request().json
        oper = request_params.get('operation')
        if oper is None:
            err_msg = ("Error: Key/value store API needs 'operation' "
                       "parameter")
            raise cfgm_common.exceptions.HttpError(400, err_msg)
        if 'key' not in request_params:
            err_msg = ("Error: Key/value store API needs 'key' parameter")
            raise cfgm_common.exceptions.HttpError(400, err_msg)
        key = request_params.get('key')
        val = request_params.get('value', '')


        # TODO move values to common
        if oper == 'STORE':
            self._db_conn.useragent_kv_store(key, val)
        elif oper == 'RETRIEVE':
            try:
                result = self._db_conn.useragent_kv_retrieve(key)
                return {'value': result}
            except NoUserAgentKey:
                raise cfgm_common.exceptions.HttpError(
                    404, "Unknown User-Agent key " + key)
        elif oper == 'DELETE':
            result = self._db_conn.useragent_kv_delete(key)
        else:
            raise cfgm_common.exceptions.HttpError(
                404, "Invalid Operation " + oper)

    # end useragent_kv_http_post

    def db_check(self):
        """ Check database for inconsistencies. No update to database """
        check_result = self._db_conn.db_check()

        return {'results': check_result}
    # end db_check

    def fetch_records(self):
        """ Retrieve and return all records """
        result = self._db_conn.db_read()
        return {'results': result}
    # end fetch_records

    def start_profile(self):
        #GreenletProfiler.start()
        pass
    # end start_profile

    def stop_profile(self):
        pass
        #GreenletProfiler.stop()
        #stats = GreenletProfiler.get_func_stats()
        #self._profile_info = stats.print_all()

        #return self._profile_info
    # end stop_profile

    def get_profile_info(self):
        return self._profile_info
    # end get_profile_info

    def get_resource_class(self, type_str):
        if type_str in self._resource_classes:
            return self._resource_classes[type_str]

        common_name = cfgm_common.utils.CamelCase(type_str)
        server_name = '%sServer' % common_name
        try:
            resource_class = getattr(vnc_cfg_types, server_name)
        except AttributeError:
            common_class = cfgm_common.utils.str_to_class(common_name,
                                                          __name__)
            if common_class is None:
                raise TypeError('Invalid type: ' + type_str)
            # Create Placeholder classes derived from Resource, <Type> so
            # resource_class methods can be invoked in CRUD methods without
            # checking for None
            resource_class = type(
                str(server_name),
                (vnc_cfg_types.Resource, common_class, object),
                {})
        resource_class.server = self
        self._resource_classes[resource_class.object_type] = resource_class
        self._resource_classes[resource_class.resource_type] = resource_class
        return resource_class
    # end get_resource_class

    def list_bulk_collection_http_post(self):
        """ List collection when requested ids don't fit in query params."""

        type = get_request().json.get('type') # e.g. virtual-network
        resource_type, r_class = self._validate_resource_type(type)

        try:
            parent_uuids = get_request().json['parent_id'].split(',')
        except KeyError:
            parent_uuids = None

        try:
            back_ref_uuids = get_request().json['back_ref_id'].split(',')
        except KeyError:
            back_ref_uuids = None

        try:
            obj_uuids = get_request().json['obj_uuids'].split(',')
        except KeyError:
            obj_uuids = None

        is_count = get_request().json.get('count', False)
        is_detail = get_request().json.get('detail', False)
        include_shared = get_request().json.get('shared', False)

        try:
            filters = utils.get_filters(get_request().json.get('filters'))
        except Exception as e:
            raise cfgm_common.exceptions.HttpError(
                400, 'Invalid filter ' + get_request().json.get('filters'))

        req_fields = get_request().json.get('fields', [])
        if req_fields:
            req_fields = req_fields.split(',')

        exclude_hrefs = get_request().json.get('exclude_hrefs', False)

        return self._list_collection(r_class.object_type, parent_uuids,
                                     back_ref_uuids, obj_uuids, is_count,
                                     is_detail, filters, req_fields,
                                     include_shared, exclude_hrefs)
    # end list_bulk_collection_http_post

    # Private Methods
    def _parse_args(self, args_str):
        '''
        Eg. python vnc_cfg_api_server.py --cassandra_server_list
                                             10.1.2.3:9160 10.1.2.4:9160
                                         --redis_server_ip 127.0.0.1
                                         --redis_server_port 6382
                                         --collectors 127.0.0.1:8086
                                         --http_server_port 8090
                                         --listen_ip_addr 127.0.0.1
                                         --listen_port 8082
                                         --admin_port 8095
                                         --region_name RegionOne
                                         --log_local
                                         --log_level SYS_DEBUG
                                         --logging_level DEBUG
                                         --logging_conf <logger-conf-file>
                                         --log_category test
                                         --log_file <stdout>
                                         --trace_file /var/log/contrail/vnc_openstack.err
                                         --use_syslog
                                         --syslog_facility LOG_USER
                                         --worker_id 1
                                         --rabbit_max_pending_updates 4096
                                         --rabbit_health_check_interval 120.0
                                         --cluster_id <testbed-name>
                                         [--auth keystone]
                                         [--default_encoding ascii ]
                                         --object_cache_size 10000
                                         --object_cache_exclude_types ''
        '''
        self._args, _ = utils.parse_args(args_str)
    # end _parse_args

    # sigchld handler is currently not engaged. See comment @sigchld
    def sigchld_handler(self):
        # DB interface initialization
        self._db_connect(reset_config=False)
        self._db_init_entries()
    # end sigchld_handler

    def sigterm_handler(self):
        exit()

    # sighup handler for applying new configs
    def sighup_handler(self):
        if self._args.conf_file:
            config = ConfigParser.SafeConfigParser()
            config.read(self._args.conf_file)
            if 'DEFAULTS' in config.sections():
                try:
                    collectors = config.get('DEFAULTS', 'collectors')
                    if type(collectors) is str:
                        collectors = collectors.split()
                        new_chksum = hashlib.md5("".join(collectors)).hexdigest()
                        if new_chksum != self._chksum:
                            self._chksum = new_chksum
                            random_collectors = random.sample(collectors, len(collectors))
                            self._sandesh.reconfig_collectors(random_collectors)
                except ConfigParser.NoOptionError as e:
                    pass
    # end sighup_handler

    def _load_extensions(self):
        try:
            conf_sections = self._args.config_sections
            self._extension_mgrs['resync'] = ExtensionManager(
                'vnc_cfg_api.resync', api_server_ip=self._args.listen_ip_addr,
                api_server_port=self._args.listen_port,
                conf_sections=conf_sections, sandesh=self._sandesh)
            self._extension_mgrs['resourceApi'] = ExtensionManager(
                'vnc_cfg_api.resourceApi',
                propagate_map_exceptions=True,
                api_server_ip=self._args.listen_ip_addr,
                api_server_port=self._args.listen_port,
                conf_sections=conf_sections, sandesh=self._sandesh)
            self._extension_mgrs['neutronApi'] = ExtensionManager(
                'vnc_cfg_api.neutronApi',
                api_server_ip=self._args.listen_ip_addr,
                api_server_port=self._args.listen_port,
                conf_sections=conf_sections, sandesh=self._sandesh,
                api_server_obj=self)
        except Exception as e:
            err_msg = cfgm_common.utils.detailed_traceback()
            self.config_log("Exception in extension load: %s" %(err_msg),
                level=SandeshLevel.SYS_ERR)
    # end _load_extensions

    def _db_connect(self, reset_config):
        cass_server_list = self._args.cassandra_server_list
        redis_server_ip = self._args.redis_server_ip
        redis_server_port = self._args.redis_server_port
        zk_server = self._args.zk_server_ip
        rabbit_servers = self._args.rabbit_server
        rabbit_port = self._args.rabbit_port
        rabbit_user = self._args.rabbit_user
        rabbit_password = self._args.rabbit_password
        rabbit_vhost = self._args.rabbit_vhost
        rabbit_ha_mode = self._args.rabbit_ha_mode
        cassandra_user = self._args.cassandra_user
        cassandra_password = self._args.cassandra_password
        obj_cache_entries = int(self._args.object_cache_entries)
        obj_cache_exclude_types = \
            [t.replace('-', '_').strip() for t in
             self._args.object_cache_exclude_types.split(',')]

        rdbms_server_list = self._args.rdbms_server_list
        rdbms_user = self._args.rdbms_user
        rdbms_password = self._args.rdbms_password
        rdbms_connection = self._args.rdbms_connection

        db_engine = self._args.db_engine
        self._db_engine = db_engine
        cred = None

        db_server_list = None
        if db_engine == 'cassandra':
            if cassandra_user is not None and cassandra_password is not None:
                cred = {'username':cassandra_user,'password':cassandra_password}
            db_server_list = cass_server_list

        if db_engine == 'rdbms':
            db_server_list = rdbms_server_list
            if rdbms_user is not None and rdbms_password is not None:
                cred = {'username': rdbms_user,'password': rdbms_password}

        self._db_conn = VncDbClient(
            self, db_server_list, rabbit_servers, rabbit_port, rabbit_user,
            rabbit_password, rabbit_vhost, rabbit_ha_mode, reset_config,
            zk_server, self._args.cluster_id, db_credential=cred,
            db_engine=db_engine, rabbit_use_ssl=self._args.rabbit_use_ssl,
            kombu_ssl_version=self._args.kombu_ssl_version,
            kombu_ssl_keyfile= self._args.kombu_ssl_keyfile,
            kombu_ssl_certfile=self._args.kombu_ssl_certfile,
            kombu_ssl_ca_certs=self._args.kombu_ssl_ca_certs,
            obj_cache_entries=obj_cache_entries,
            obj_cache_exclude_types=obj_cache_exclude_types, connection=rdbms_connection)

        #TODO refacter db connection management.
        self._addr_mgmt._get_db_conn()
    # end _db_connect

    def _ensure_id_perms_present(self, obj_uuid, obj_dict):
        """
        Called at resource creation to ensure that id_perms is present in obj
        """
        # retrieve object and permissions
        id_perms = self._get_default_id_perms()

        if (('id_perms' not in obj_dict) or
                (obj_dict['id_perms'] is None)):
            # Resource creation
            if obj_uuid is None:
                obj_dict['id_perms'] = id_perms
                return

            return

        # retrieve the previous version of the id_perms
        # from the database and update the id_perms with
        # them.
        if obj_uuid is not None:
            try:
                old_id_perms = self._db_conn.uuid_to_obj_perms(obj_uuid)
                for field, value in old_id_perms.items():
                    if value is not None:
                        id_perms[field] = value
            except NoIdError:
                pass

        # not all fields can be updated
        if obj_uuid:
            field_list = ['enable', 'description']
        else:
            field_list = ['enable', 'description', 'user_visible', 'creator']

        # Start from default and update from obj_dict
        req_id_perms = obj_dict['id_perms']
        for key in field_list:
            if key in req_id_perms:
                id_perms[key] = req_id_perms[key]
        # TODO handle perms present in req_id_perms

        obj_dict['id_perms'] = id_perms
    # end _ensure_id_perms_present

    def _get_default_id_perms(self):
        id_perms = copy.deepcopy(Provision.defaults.perms)
        id_perms_json = json.dumps(id_perms, default=lambda o: dict((k, v)
                                   for k, v in o.__dict__.iteritems()))
        id_perms_dict = json.loads(id_perms_json)
        return id_perms_dict
    # end _get_default_id_perms

    def _ensure_perms2_present(self, obj_type, obj_uuid, obj_dict,
                               project_id=None):
        """
        Called at resource creation to ensure that id_perms is present in obj
        """
        # retrieve object and permissions
        perms2 = self._get_default_perms2()

        # set ownership of object to creator tenant
        if obj_type == 'project' and 'uuid' in obj_dict:
            perms2['owner'] = str(obj_dict['uuid']).replace('-','')
        elif project_id:
            perms2['owner'] = project_id

        # set ownership of object to creator tenant
        if obj_type == 'project' and 'uuid' in obj_dict:
            perms2['owner'] = str(obj_dict['uuid']).replace('-','')
        elif project_id:
            perms2['owner'] = project_id

        if (('perms2' not in obj_dict) or
                (obj_dict['perms2'] is None)):
            # Resource creation
            if obj_uuid is None:
                obj_dict['perms2'] = perms2
                return (True, "")
            # Resource already exist
            try:
                obj_dict['perms2'] = self._db_conn.uuid_to_obj_perms2(obj_uuid)
            except NoIdError:
                obj_dict['perms2'] = perms2
            return (True, "")

        # retrieve the previous version of the perms2
        # from the database and update the perms2 with
        # them.
        if obj_uuid is not None:
            try:
                old_perms2 = self._db_conn.uuid_to_obj_perms2(obj_uuid)
                for field, value in old_perms2.items():
                    if value is not None:
                        perms2[field] = value
            except NoIdError:
                pass

        # Start from default and update from obj_dict
        req_perms2 = obj_dict['perms2']
        for key in req_perms2:
            perms2[key] = req_perms2[key]
        # TODO handle perms2 present in req_perms2

        obj_dict['perms2'] = perms2

        # ensure is_shared and global_access are consistent
        shared = obj_dict.get('is_shared', None)
        gaccess = obj_dict['perms2'].get('global_access', None)
        if gaccess is not None and shared is not None and shared != (gaccess != 0):
            error = "Inconsistent is_shared (%s a) and global_access (%s)" % (shared, gaccess)
            return (False, (400, error))
        return (True, "")
    # end _ensure_perms2_present

    def _get_default_perms2(self):
        perms2 = copy.deepcopy(Provision.defaults.perms2)
        perms2_json = json.dumps(perms2, default=lambda o: dict((k, v)
                                   for k, v in o.__dict__.iteritems()))
        perms2_dict = json.loads(perms2_json)
        return perms2_dict
    # end _get_default_perms2

    def _db_init_entries(self):
        # create singleton defaults if they don't exist already in db
        glb_sys_cfg = self._create_singleton_entry(
            GlobalSystemConfig(autonomous_system=64512,
                               config_version=CONFIG_VERSION))
        def_domain = self._create_singleton_entry(Domain())
        ip_fab_vn = self._create_singleton_entry(
            VirtualNetwork(cfgm_common.IP_FABRIC_VN_FQ_NAME[-1]))
        self._create_singleton_entry(
            RoutingInstance('__default__', ip_fab_vn,
                routing_instance_is_default=True))
        link_local_vn = self._create_singleton_entry(
            VirtualNetwork(cfgm_common.LINK_LOCAL_VN_FQ_NAME[-1]))
        self._create_singleton_entry(
            RoutingInstance('__link_local__', link_local_vn,
                routing_instance_is_default=True))
        try:
            self._create_singleton_entry(
                RoutingInstance('default-virtual-network',
                    routing_instance_is_default=True))
        except Exception as e:
            self.config_log('error while creating primary routing instance for'
                            'default-virtual-network: ' + str(e),
                            level=SandeshLevel.SYS_NOTICE)

        self._create_singleton_entry(DiscoveryServiceAssignment())
        self._create_singleton_entry(GlobalQosConfig())

        if int(self._args.worker_id) == 0:
            self._db_conn.db_resync()

        # make default ipam available across tenants for backward compatability
        obj_type = 'network_ipam'
        fq_name = ['default-domain', 'default-project', 'default-network-ipam']
        obj_uuid = self._db_conn.fq_name_to_uuid(obj_type, fq_name)
        (ok, obj_dict) = self._db_conn.dbe_read(obj_type, obj_uuid,
                                                obj_fields=['perms2'])
        obj_dict['perms2']['global_access'] = PERMS_RX
        self._db_conn.dbe_update(obj_type, obj_uuid, obj_dict)
    # end _db_init_entries

    # generate default rbac group rule
    def _create_default_rbac_rule(self):
        obj_type = 'api_access_list'
        fq_name = ['default-global-system-config', 'default-api-access-list']
        try:
            id = self._db_conn.fq_name_to_uuid(obj_type, fq_name)
            return
        except NoIdError:
            pass

        # allow full access to cloud admin
        rbac_rules = [
            {
                'rule_object':'fqname-to-id',
                'rule_field': '',
                'rule_perms': [{'role_name':'*', 'role_crud':'CRUD'}]
            },
            {
                'rule_object':'id-to-fqname',
                'rule_field': '',
                'rule_perms': [{'role_name':'*', 'role_crud':'CRUD'}]
            },
            {
                'rule_object':'useragent-kv',
                'rule_field': '',
                'rule_perms': [{'role_name':'*', 'role_crud':'CRUD'}]
            },
            {
                'rule_object':'documentation',
                'rule_field': '',
                'rule_perms': [{'role_name':'*', 'role_crud':'R'}]
            },
            {
                'rule_object':'/',
                'rule_field': '',
                'rule_perms': [{'role_name':'*', 'role_crud':'R'}]
            },
        ]

        rge = RbacRuleEntriesType([])
        for rule in rbac_rules:
            rule_perms = [RbacPermType(role_name=p['role_name'], role_crud=p['role_crud']) for p in rule['rule_perms']]
            rbac_rule = RbacRuleType(rule_object=rule['rule_object'],
                rule_field=rule['rule_field'], rule_perms=rule_perms)
            rge.add_rbac_rule(rbac_rule)

        rge_dict = rge.exportDict('')
        glb_rbac_cfg = ApiAccessList(parent_type='global-system-config',
            fq_name=fq_name, api_access_list_entries = rge_dict)

        try:
            self._create_singleton_entry(glb_rbac_cfg)
        except Exception as e:
            err_msg = 'Error creating default api access list object'
            err_msg += cfgm_common.utils.detailed_traceback()
            self.config_log(err_msg, level=SandeshLevel.SYS_ERR)
    # end _create_default_rbac_rule

    def _resync_domains_projects(self, ext):
        if hasattr(ext.obj, 'resync_domains_projects'):
            ext.obj.resync_domains_projects()
    # end _resync_domains_projects

    def _create_singleton_entry(self, singleton_obj):
        s_obj = singleton_obj
        obj_type = s_obj.object_type
        fq_name = s_obj.get_fq_name()

        # TODO remove backward compat create mapping in zk
        # for singleton START
        try:
            cass_uuid = self._db_conn._object_db.fq_name_to_uuid(obj_type, fq_name)
            try:
                zk_uuid = self._db_conn.fq_name_to_uuid(obj_type, fq_name)
            except NoIdError:
                # doesn't exist in zookeeper but does so in cassandra,
                # migrate this info to zookeeper
                self._db_conn._zk_db.create_fq_name_to_uuid_mapping(obj_type, fq_name, str(cass_uuid))
        except NoIdError:
            # doesn't exist in cassandra as well as zookeeper, proceed normal
            pass
        # TODO backward compat END


        # create if it doesn't exist yet
        try:
            id = self._db_conn.fq_name_to_uuid(obj_type, fq_name)
        except NoIdError:
            obj_dict = s_obj.serialize_to_json()
            obj_dict['id_perms'] = self._get_default_id_perms()
            obj_dict['perms2'] = self._get_default_perms2()
            (ok, result) = self._db_conn.dbe_alloc(obj_type, obj_dict)
            obj_id = result
            # For virtual networks, allocate an ID
            if obj_type == 'virtual_network':
                vn_id = self.alloc_vn_id(s_obj.get_fq_name_str())
                obj_dict['virtual_network_network_id'] = vn_id
            self._db_conn.dbe_create(obj_type, obj_id, obj_dict)
            self.create_default_children(obj_type, s_obj)

        return s_obj
    # end _create_singleton_entry

    def _list_collection(self, obj_type, parent_uuids=None,
                         back_ref_uuids=None, obj_uuids=None,
                         is_count=False, is_detail=False, filters=None,
                         req_fields=None, include_shared=False,
                         exclude_hrefs=False):
        resource_type, r_class = self._validate_resource_type(obj_type)
        is_admin = self.is_admin_request()
        if is_admin:
            field_names = req_fields
        else:
            field_names = [u'id_perms'] + (req_fields or [])

        (ok, result) = self._db_conn.dbe_list(obj_type,
                             parent_uuids, back_ref_uuids, obj_uuids, is_count and self.is_admin_request(),
                             filters, is_detail=is_detail, field_names=field_names,
                             include_shared=include_shared)
        if not ok:
            self.config_object_error(None, None, '%ss' %(obj_type),
                                     'dbe_list', result)
            raise cfgm_common.exceptions.HttpError(404, result)

        # If only counting, return early
        if is_count and self.is_admin_request():
            return {'%ss' %(resource_type): {'count': result}}

        allowed_fields = ['uuid', 'href', 'fq_name'] + (req_fields or [])
        obj_dicts = []
        if is_admin:
            for obj_result in result:
                if not exclude_hrefs:
                    obj_result['href'] = self.generate_url(
                        resource_type, obj_result['uuid'])
                if is_detail:
                    obj_result['name'] = obj_result['fq_name'][-1]
                    obj_dicts.append({resource_type: obj_result})
                else:
                    obj_dicts.append(obj_result)
        else:
            for obj_result in result:
                # TODO(nati) we should do this using sql query
                id_perms = obj_result.get('id_perms')

                if not id_perms:
                    # It is possible that the object was deleted, but received
                    # an update after that. We need to ignore it for now. In
                    # future, we should clean up such stale objects
                    continue

                if not id_perms.get('user_visible', True):
                    # skip items not authorized
                    continue

                (ok, status) = self._permissions.check_perms_read(
                        get_request(), obj_result['uuid'],
                        obj_result['id_perms'])
                if not ok and status[0] == 403:
                    continue

                obj_dict = {}

                if is_detail:
                    obj_result = self.obj_view(resource_type, obj_result)
                    obj_result['name'] = obj_result['fq_name'][-1]
                    obj_dict.update(obj_result)
                    obj_dicts.append({resource_type: obj_dict})
                else:
                    obj_dict.update(obj_result)
                    for key in obj_dict.keys():
                        if not key in allowed_fields:
                            del obj_dict[key]
                    if obj_dict.get('id_perms') and not 'id_perms' in allowed_fields:
                        del obj_dict['id_perms']
                    obj_dicts.append(obj_dict)

                if not exclude_hrefs:
                    obj_dict['href'] = self.generate_url(resource_type, obj_result['uuid'])

        if is_count:
            return {'%ss' %(resource_type): {'count': len(obj_dicts)}}
        return {'%ss' %(resource_type): obj_dicts}
    # end _list_collection

    def get_db_connection(self):
        return self._db_conn
    # end get_db_connection

    def generate_url(self, resource_type, obj_uuid):
        try:
            url_parts = get_request().urlparts
            return '%s://%s/%s/%s'\
                % (url_parts.scheme, url_parts.netloc, resource_type, obj_uuid)
        except Exception as e:
            return '%s/%s/%s' % (self._base_url, resource_type, obj_uuid)
    # end generate_url

    def generate_hrefs(self, resource_type, obj_dict):
        # return a copy of obj_dict with href keys for:
        # self, parent, children, refs, backrefs
        # don't update obj_dict as it may be cached object
        r_class = self.get_resource_class(resource_type)

        ret_obj_dict = obj_dict.copy()
        ret_obj_dict['href'] = self.generate_url(
            resource_type, obj_dict['uuid'])

        try:
            ret_obj_dict['parent_href'] = self.generate_url(
                obj_dict['parent_type'], obj_dict['parent_uuid'])
        except KeyError:
            # No parent
            pass

        for child_field, child_field_info in \
                r_class.children_field_types.items():
            try:
                children = obj_dict[child_field]
                child_type = child_field_info[0]
                ret_obj_dict[child_field] = [
                    dict(c, href=self.generate_url(child_type, c['uuid']))
                    for c in children]
            except KeyError:
                # child_field doesn't exist in original
                pass
        # end for all child fields

        for ref_field, ref_field_info in r_class.ref_field_types.items():
            try:
                refs = obj_dict[ref_field]
                ref_type = ref_field_info[0]
                ret_obj_dict[ref_field] = [
                    dict(r, href=self.generate_url(ref_type, r['uuid']))
                    for r in refs]
            except KeyError:
                # ref_field doesn't exist in original
                pass
        # end for all ref fields

        for backref_field, backref_field_info in \
                r_class.backref_field_types.items():
            try:
                backrefs = obj_dict[backref_field]
                backref_type = backref_field_info[0]
                ret_obj_dict[backref_field] = [
                    dict(b, href=self.generate_url(backref_type, b['uuid']))
                    for b in backrefs]
            except KeyError:
                # backref_field doesn't exist in original
                pass
        # end for all backref fields

        return ret_obj_dict
    # end generate_hrefs

    def config_object_error(self, id, fq_name_str, obj_type,
                            operation, err_str):
        apiConfig = VncApiCommon()
        if obj_type is not None:
            apiConfig.object_type = obj_type
        apiConfig.identifier_name = fq_name_str
        apiConfig.identifier_uuid = id
        apiConfig.operation = operation
        if err_str:
            apiConfig.error = "%s:%s" % (obj_type, err_str)
        self._set_api_audit_info(apiConfig)

        log = VncApiConfigLog(api_log=apiConfig, sandesh=self._sandesh)
        log.send(sandesh=self._sandesh)
    # end config_object_error

    def config_log(self, msg_str, level=SandeshLevel.SYS_INFO):
        errcls = {
            SandeshLevel.SYS_DEBUG: VncApiDebug,
            SandeshLevel.SYS_INFO: VncApiInfo,
            SandeshLevel.SYS_NOTICE: VncApiNotice,
            SandeshLevel.SYS_ERR: VncApiError,
        }
        errcls.get(level, VncApiError)(
            api_msg=msg_str, level=level, sandesh=self._sandesh).send(
                sandesh=self._sandesh)
    # end config_log

    def _set_api_audit_info(self, apiConfig):
        apiConfig.url = get_request().url
        apiConfig.remote_ip = get_request().headers.get('Host')
        useragent = get_request().headers.get('X-Contrail-Useragent')
        if not useragent:
            useragent = get_request().headers.get('User-Agent')
        apiConfig.useragent = useragent
        apiConfig.user = get_request().headers.get('X-User-Name')
        apiConfig.project = get_request().headers.get('X-Project-Name')
        apiConfig.domain = get_request().headers.get('X-Domain-Name', 'None')
        if apiConfig.domain.lower() == 'none':
            apiConfig.domain = 'default-domain'
        if int(get_request().headers.get('Content-Length', 0)) > 0:
            try:
                body = json.dumps(get_request().json)
            except:
                body = str(get_request().json)
            apiConfig.body = body
    # end _set_api_audit_info

    # uuid is parent's for collections
    def _get_common(self, request, uuid=None):
        # TODO check api + resource perms etc.
        if self.is_multi_tenancy_set() and uuid:
            if isinstance(uuid, list):
                for u_id in uuid:
                    ok, result = self._permissions.check_perms_read(request,
                                                                    u_id)
                    if not ok:
                        return ok, result
            else:
                return self._permissions.check_perms_read(request, uuid)

        return (True, '')
    # end _get_common

    def _put_common(
            self, api_name, obj_type, obj_uuid, db_obj_dict, req_obj_dict=None,
            req_prop_coll_updates=None):

        obj_fq_name = db_obj_dict.get('fq_name', 'missing-fq-name')
        # ZK and rabbitmq should be functional
        self._ensure_services_conn(
            api_name, obj_type, obj_uuid, obj_fq_name)

        resource_type, r_class = self._validate_resource_type(obj_type)
        try:
            self._extension_mgrs['resourceApi'].map_method(
                'pre_%s_update' %(obj_type), obj_uuid, req_obj_dict)
        except RuntimeError:
            # lack of registered extension leads to RuntimeError
            pass
        except Exception as e:
            err_msg = 'In pre_%s_update an extension had error for %s' \
                      %(obj_type, req_obj_dict)
            err_msg += cfgm_common.utils.detailed_traceback()
            self.config_log(err_msg, level=SandeshLevel.SYS_NOTICE)

        db_conn = self._db_conn

        # check visibility
        if (not db_obj_dict['id_perms'].get('user_visible', True) and
            not self.is_admin_request()):
            result = 'This object is not visible by users: %s' % obj_uuid
            self.config_object_error(obj_uuid, None, obj_type, api_name, result)
            raise cfgm_common.exceptions.HttpError(404, result)

        # properties validator (for collections validation in caller)
        if req_obj_dict is not None:
            ok, result = self._validate_props_in_request(r_class, req_obj_dict)
            if not ok:
                result = 'Bad property in %s: %s' %(api_name, result)
                raise cfgm_common.exceptions.HttpError(400, result)

        # references validator
        if req_obj_dict is not None:
            ok, result = self._validate_refs_in_request(r_class, req_obj_dict)
            if not ok:
                result = 'Bad reference in %s: %s' %(api_name, result)
                raise cfgm_common.exceptions.HttpError(400, result)

        # common handling for all resource put
        request = get_request()
        fq_name_str = ":".join(obj_fq_name or [])
        if req_obj_dict:
            if 'id_perms' in req_obj_dict and req_obj_dict['id_perms']['uuid']:
                if not self._db_conn.match_uuid(req_obj_dict, obj_uuid):
                    log_msg = 'UUID mismatch from %s:%s' \
                        % (request.environ['REMOTE_ADDR'],
                           request.environ['HTTP_USER_AGENT'])
                    self.config_object_error(
                        obj_uuid, fq_name_str, obj_type, 'put', log_msg)
                    self._db_conn.set_uuid(obj_type, req_obj_dict,
                                           uuid.UUID(obj_uuid),
                                           do_lock=False)

            # Ensure object has at least default permissions set
            self._ensure_id_perms_present(obj_uuid, req_obj_dict)

        apiConfig = VncApiCommon()
        apiConfig.object_type = obj_type
        apiConfig.identifier_name = fq_name_str
        apiConfig.identifier_uuid = obj_uuid
        apiConfig.operation = api_name
        self._set_api_audit_info(apiConfig)
        log = VncApiConfigLog(api_log=apiConfig,
                sandesh=self._sandesh)
        log.send(sandesh=self._sandesh)

        if self.is_multi_tenancy_set():
            ok, result = self._permissions.check_perms_write(request, obj_uuid)
            if not ok:
                (code, msg) = result
                self.config_object_error(
                    obj_uuid, fq_name_str, obj_type, api_name, msg)
                raise cfgm_common.exceptions.HttpError(code, msg)

        # Validate perms on references
        if req_obj_dict is not None:
            try:
                self._validate_perms_in_request(
                    r_class, obj_type, req_obj_dict)
            except NoIdError:
                raise cfgm_common.exceptions.HttpError(400,
                    'Unknown reference in resource update %s %s.'
                    %(obj_type, req_obj_dict))

        # State modification starts from here. Ensure that cleanup is done for all state changes
        cleanup_on_failure = []
        if req_obj_dict is not None:
            req_obj_dict['uuid'] = obj_uuid

        def stateful_update():
            get_context().set_state('PRE_DBE_UPDATE')
            # type-specific hook
            (ok, result) = r_class.pre_dbe_update(
                obj_uuid, obj_fq_name, req_obj_dict or {}, self._db_conn,
                prop_collection_updates=req_prop_coll_updates)
            if not ok:
                return (ok, result)

            get_context().set_state('DBE_UPDATE')
            if req_obj_dict:
                (ok, result) = db_conn.dbe_update(obj_type, obj_uuid, req_obj_dict)
            elif req_prop_coll_updates:
                (ok, result) = db_conn.prop_collection_update(
                    obj_type, obj_uuid, req_prop_coll_updates)
            if not ok:
                return (ok, result)

            get_context().set_state('POST_DBE_UPDATE')
            # type-specific hook
            (ok, result) = r_class.post_dbe_update(
                obj_uuid, obj_fq_name, req_obj_dict or {}, self._db_conn,
                prop_collection_updates=req_prop_coll_updates)
            if not ok:
                return (ok, result)

            return (ok, result)
        # end stateful_update

        try:
            ok, result = stateful_update()
        except Exception as e:
            ok = False
            err_msg = cfgm_common.utils.detailed_traceback()
            result = (500, err_msg)
        if not ok:
            self.undo(result, obj_type, id=obj_uuid)
            code, msg = result
            raise cfgm_common.exceptions.HttpError(code, msg)

        try:
            self._extension_mgrs['resourceApi'].map_method(
                'post_%s_update' %(obj_type), obj_uuid,
                 req_obj_dict, db_obj_dict)
        except RuntimeError:
            # lack of registered extension leads to RuntimeError
            pass
        except Exception as e:
            err_msg = 'In post_%s_update an extension had error for %s' \
                      %(obj_type, req_obj_dict)
            err_msg += cfgm_common.utils.detailed_traceback()
            self.config_log(err_msg, level=SandeshLevel.SYS_NOTICE)
    # end _put_common

    # parent_type needed for perms check. None for derived objects (eg.
    # routing-instance)
    def _delete_common(self, request, obj_type, uuid, parent_uuid):
        # If not connected to zookeeper do not allow operations that
        # causes the state change
        if not self._db_conn._zk_db.is_connected():
            return (False,
                    (503, "Not connected to zookeeper. Not able to perform requested action"))

        # If there are too many pending updates to rabbit, do not allow
        # operations that cause state change
        npending = self._db_conn.dbe_oper_publish_pending()
        if (npending >= int(self._args.rabbit_max_pending_updates)):
            err_str = str(MaxRabbitPendingError(npending))
            return (False, (500, err_str))

        fq_name = self._db_conn.uuid_to_fq_name(uuid)
        apiConfig = VncApiCommon()
        apiConfig.object_type = obj_type
        apiConfig.identifier_name=':'.join(fq_name)
        apiConfig.identifier_uuid = uuid
        apiConfig.operation = 'delete'
        self._set_api_audit_info(apiConfig)
        log = VncApiConfigLog(api_log=apiConfig, sandesh=self._sandesh)
        log.send(sandesh=self._sandesh)

        # TODO check api + resource perms etc.
        if not self.is_multi_tenancy_set() or not parent_uuid:
            return (True, '')

        """
        Validate parent allows write access. Implicitly trust
        parent info in the object since coming from our DB.
        """
        return self._permissions.check_perms_delete(request, obj_type, uuid,
                                                    parent_uuid)
    # end _http_delete_common

    def _post_validate(self, obj_type=None, obj_dict=None):
        if not obj_dict:
            return

        def _check_field_present(fname):
            fval = obj_dict.get(fname)
            if not fval:
                raise cfgm_common.exceptions.HttpError(
                    400, "Bad Request, no %s in POST body" %(fname))
            return fval
        fq_name = _check_field_present('fq_name')

        # well-formed name checks
        if illegal_xml_chars_RE.search(fq_name[-1]):
            raise cfgm_common.exceptions.HttpError(400,
                "Bad Request, name has illegal xml characters")
        if obj_type == 'route_target':
            invalid_chars = self._INVALID_NAME_CHARS - set(':')
        else:
            invalid_chars = self._INVALID_NAME_CHARS
        if any((c in invalid_chars) for c in fq_name[-1]):
            raise cfgm_common.exceptions.HttpError(400,
                "Bad Request, name has one of invalid chars %s"
                %(invalid_chars))
    # end _post_validate

    def _post_common(self, obj_type, obj_dict):
        self._ensure_services_conn(
            'http_post', obj_type, obj_fq_name=obj_dict.get('fq_name'))

        if not obj_dict:
            # TODO check api + resource perms etc.
            return (True, None)

        # Fail if object exists already
        try:
            obj_uuid = self._db_conn.fq_name_to_uuid(
                obj_type, obj_dict['fq_name'])
            raise cfgm_common.exceptions.HttpError(
                409, '' + pformat(obj_dict['fq_name']) +
                ' already exists with uuid: ' + obj_uuid)
        except NoIdError:
            pass

        # Ensure object has at least default permissions set
        self._ensure_id_perms_present(None, obj_dict)
        self._ensure_perms2_present(obj_type, None, obj_dict,
            get_request().headers.environ.get('HTTP_X_PROJECT_ID', None))

        # TODO check api + resource perms etc.

        uuid_in_req = obj_dict.get('uuid', None)

        # Set the display name
        if (('display_name' not in obj_dict) or
            (obj_dict['display_name'] is None)):
            obj_dict['display_name'] = obj_dict['fq_name'][-1]

        fq_name_str = ":".join(obj_dict['fq_name'])
        apiConfig = VncApiCommon()
        apiConfig.object_type = obj_type
        apiConfig.identifier_name=fq_name_str
        apiConfig.identifier_uuid = uuid_in_req
        apiConfig.operation = 'post'
        try:
            body = json.dumps(get_request().json)
        except:
            body = str(get_request().json)
        apiConfig.body = body
        if uuid_in_req:
            if uuid_in_req != str(uuid.UUID(uuid_in_req)):
                bottle.abort(400, 'Invalid UUID format: ' + uuid_in_req)
            try:
                fq_name = self._db_conn.uuid_to_fq_name(uuid_in_req)
                raise cfgm_common.exceptions.HttpError(
                    409, uuid_in_req + ' already exists with fq_name: ' +
                    pformat(fq_name))
            except NoIdError:
                pass
            apiConfig.identifier_uuid = uuid_in_req

        self._set_api_audit_info(apiConfig)
        log = VncApiConfigLog(api_log=apiConfig, sandesh=self._sandesh)
        log.send(sandesh=self._sandesh)

        return (True, uuid_in_req)
    # end _post_common

    def reset(self):
        # cleanup internal state/in-flight operations
        if self._db_conn:
            self._db_conn.reset()
    # end reset

    # allocate block of IP addresses from VN. Subnet info expected in request
    # body
    def vn_ip_alloc_http_post(self, id):
        try:
            vn_fq_name = self._db_conn.uuid_to_fq_name(id)
        except NoIdError:
            raise cfgm_common.exceptions.HttpError(
                404, 'Virtual Network ' + id + ' not found!')

        # expected format {"subnet_list" : "2.1.1.0/24", "count" : 4}
        req_dict = get_request().json
        count = req_dict.get('count', 1)
        subnet = req_dict.get('subnet')
        family = req_dict.get('family')
        try:
            result = vnc_cfg_types.VirtualNetworkServer.ip_alloc(
                vn_fq_name, subnet, count, family)
        except vnc_addr_mgmt.AddrMgmtSubnetUndefined as e:
            raise cfgm_common.exceptions.HttpError(404, str(e))
        except vnc_addr_mgmt.AddrMgmtSubnetExhausted as e:
            raise cfgm_common.exceptions.HttpError(409, str(e))

        return result
    # end vn_ip_alloc_http_post

    # free block of ip addresses to subnet
    def vn_ip_free_http_post(self, id):
        try:
            vn_fq_name = self._db_conn.uuid_to_fq_name(id)
        except NoIdError:
            raise cfgm_common.exceptions.HttpError(
                404, 'Virtual Network ' + id + ' not found!')

        """
          {
            "subnet" : "2.1.1.0/24",
            "ip_addr": [ "2.1.1.239", "2.1.1.238", "2.1.1.237", "2.1.1.236" ]
          }
        """

        req_dict = get_request().json
        ip_list = req_dict['ip_addr'] if 'ip_addr' in req_dict else []
        result = vnc_cfg_types.VirtualNetworkServer.ip_free(
            vn_fq_name, ip_list)
        return result
    # end vn_ip_free_http_post

    # return no. of  IP addresses from VN/Subnet
    def vn_subnet_ip_count_http_post(self, id):
        try:
            vn_fq_name = self._db_conn.uuid_to_fq_name(id)
        except NoIdError:
            raise cfgm_common.exceptions.HttpError(
                404, 'Virtual Network ' + id + ' not found!')

        # expected format {"subnet_list" : ["2.1.1.0/24", "1.1.1.0/24"]
        req_dict = get_request().json
        try:
            (ok, result) = self._db_conn.dbe_read('virtual_network', id)
        except NoIdError as e:
            raise cfgm_common.exceptions.HttpError(404, str(e))
        except Exception as e:
            ok = False
            result = cfgm_common.utils.detailed_traceback()
        if not ok:
            raise cfgm_common.exceptions.HttpError(500, result)

        obj_dict = result
        subnet_list = req_dict[
            'subnet_list'] if 'subnet_list' in req_dict else []
        result = vnc_cfg_types.VirtualNetworkServer.subnet_ip_count(
            vn_fq_name, subnet_list)
        return result
    # end vn_subnet_ip_count_http_post

    def set_mt(self, multi_tenancy):
        pipe_start_app = self.get_pipe_start_app()
        try:
            pipe_start_app.set_mt(multi_tenancy)
        except AttributeError:
            pass
        self._args.multi_tenancy = multi_tenancy
    # end

    # check if token validatation needed
    def is_multi_tenancy_set(self):
        return self.aaa_mode != 'no-auth'

    def is_rbac_enabled(self):
        return self.aaa_mode == 'rbac'

    def mt_http_get(self):
        pipe_start_app = self.get_pipe_start_app()
        mt = self.is_multi_tenancy_set()
        try:
            mt = pipe_start_app.get_mt()
        except AttributeError:
            pass
        return {'enabled': mt}
    # end

    def mt_http_put(self):
        multi_tenancy = get_request().json['enabled']
        user_token = get_request().get_header('X-Auth-Token')
        if user_token is None:
            raise cfgm_common.exceptions.HttpError(403, " Permission denied")

        data = self._auth_svc.verify_signed_token(user_token)
        if data is None:
            raise cfgm_common.exceptions.HttpError(403, " Permission denied")

        self.set_mt(multi_tenancy)
        return {'enabled': self.is_multi_tenancy_set()}
    # end

    @property
    def aaa_mode(self):
        return self._args.aaa_mode

    @aaa_mode.setter
    def aaa_mode(self, mode):
        self._args.aaa_mode = mode

    # indication if multi tenancy with rbac is enabled or disabled
    def aaa_mode_http_get(self):
        return {'aaa-mode': self.aaa_mode}

    def aaa_mode_http_put(self):
        aaa_mode = get_request().json['aaa-mode']
        if aaa_mode not in cfgm_common.AAA_MODE_VALID_VALUES:
            raise ValueError('Invalid aaa-mode %s' % aaa_mode)

        if not self._auth_svc.validate_user_token(get_request()):
            raise cfgm_common.exceptions.HttpError(403, " Permission denied")
        if not self.is_admin_request():
            raise cfgm_common.exceptions.HttpError(403, " Permission denied")

        self.aaa_mode = aaa_mode
        if self.is_rbac_enabled():
            self._create_default_rbac_rule()
        return {'aaa-mode': self.aaa_mode}
    # end

    @property
    def cloud_admin_role(self):
        return self._args.cloud_admin_role

    @property
    def global_read_only_role(self):
        return self._args.global_read_only_role

    def keystone_version(self):
        k_v = 'v2.0'
        try:
            if 'v3' in self._args.auth_url:
                k_v = 'v3'
        except AttributeError:
            pass
        return k_v

# end class VncApiServer

def main(args_str=None, server=None):
    vnc_api_server = server

    pipe_start_app = vnc_api_server.get_pipe_start_app()
    server_ip = vnc_api_server.get_listen_ip()
    server_port = vnc_api_server.get_server_port()

    """ @sigchld
    Disable handling of SIG_CHLD for now as every keystone request to validate
    token sends SIG_CHLD signal to API server.
    """
    #hub.signal(signal.SIGCHLD, vnc_api_server.sigchld_handler)
    hub.signal(signal.SIGTERM, vnc_api_server.sigterm_handler)
    hub.signal(signal.SIGHUP, vnc_api_server.sighup_handler)
    if pipe_start_app is None:
        pipe_start_app = vnc_api_server.api_bottle
    try:
        bottle.run(app=pipe_start_app, host=server_ip, port=server_port,
                   server=get_bottle_server(server._args.max_requests))
    except KeyboardInterrupt:
        # quietly handle Ctrl-C
        pass
    except:
        # dump stack on all other exceptions
        raise
    finally:
        # always cleanup gracefully
        vnc_api_server.reset()

# end main

def server_main(args_str=None):
    vnc_cgitb.enable(format='text')

    main(args_str, VncApiServer(args_str))
#server_main

if __name__ == "__main__":
    server_main()
