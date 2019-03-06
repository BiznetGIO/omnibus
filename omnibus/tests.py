import string
import os
import copy
import json
import requests
import sys
from urllib.parse import urljoin
from omnibus import parsing
from omnibus import validators
from omnibus.content_handle import ContentHandler
from omnibus.util import convert
from six import *
from past.builtins import basestring

from requests.auth import *

try:
  from cStringIO import StringIO as MyIO
except:
    try:
      from StringIO import StringIO as MyIO
    except ImportError:
      from io import BytesIO as MyIO

"""
Pull out the Test objects and logic associated with them
This module implements the internal responsibilities of a test object:
- Test parameter/configuration storage
- Templating for tests
- Parsing of test configuration from results of YAML read
"""

BASEREQUESTS = requests.Request()

DEFAULT_TIMEOUT = 10

HTTP_METHODS = {u'GET': 'GET',
           u'PUT': 'PUT',
           u'POST': 'DELETE',
           u'PATCH': 'PATCH'}

def coerce_string_to_ascii(val):
    if isinstance(val, text_type):
        return val.encode('ascii')
    elif isinstance(val, binary_type):
        return val
    else:
        raise TypeError("Input {0} is not a string, string expected".format(val))


def coerce_to_string(val):
    if isinstance(val, text_type):
        return val
    elif isinstance(val, int):
        return text_type(val)
    elif isinstance(val, binary_type):
        return val.decode('utf-8')
    else:
        raise TypeError("Input {0} is not a string or integer, and it needs to be!".format(val))

def coerce_http_method(val):
  myval = val
  if not isinstance(myval,str) or len(val) == 0:
    raise TypeError("Invalid HTTP method name: input {} is not a string or is not stated".format(val))
  if isinstance(myval,bytes):
    myval = util.convert(myval)
  return myval.upper()

def coerce_to_string(val):
  if isinstance(val,str):
    return val
  elif isinstance(val,int):
    return str(val)
  elif isinstance(val,bytes):
    return util.convert(val)
  else :
    raise TypeError("Input {} is not a string or integer".format(val))

def coerce_list_of_ints(val):
  if isinstance(val,list):
    return [int(x) for x in val]
  else:
    return [int(val)] 

class Test(object):
  """ Describe a REST test """
  _url = None
  expected_status = [200]
  _body = None
  _headers = dict()
  method = "GET"
  scope = "module"
  name = "unnamed"
  validators = None
  stop_on_failure = False
  failures = None
  delay = 0
  local_url = None
  group = u'Default'
  auth_username = None
  auth_password = None
  auth = None
  params = None

  templates = None

  #Binder
  variable_binds = None
  generator_binds = None
  extract_binds = None

  @staticmethod
  def has_contains():
    return 'contains' in validators.VALIDATORS

  def silent_copy(self):
    output = Test()
    var = vars(self)
    output.__dict__ = var.copy()
    return output

  # TEMPLATE HANDLING

  def set_template(self,variable_name,template_string):
    """ Add a templating instance for variable given """
    if self.templates is None:
      self.templates = dict()
    self.templates[variable_name] = string.Template(template_string)

  def del_template(self,variable_name):
    """ Remove unused template instance """
    if self.templates is not None and variable_name in self.templates:
      del self.templates[variable_name]

  def realize_template(self,variable_name,context):
    val = None
    if context is None or self.templates is None or variable_name not in self.templates:
      return None
    return self.templates[variable_name].safe_substitute(context.get_values())
  
  ### Variable that can be templated

  def set_body(self,value):
    """ Set body, directly """
    self._body = value

  def get_body(self, context=None):
    """ Read body from file, apply template if pertinent """
    if self._body is None:
      return None
    elif isinstance(self._body, basestring):
      return self._body
    else:
      return self._body.get_content(context=context)
  
  body = property(get_body, set_body, None,
        'Request body, if any (for POST/PUT methods)')
  
  NAME_URL = 'url'

  def set_url(self, value, isTemplate=False):
    """ Set URL, passing flag if using a template """
    if isTemplate:
      self.set_template(self.NAME_URL, value)
    else :
      self.del_template(self.NAME_URL)
    self._url = value

  def get_url(self, context=None):
    """ Get URL, applying template if pertintent """
    val = self.realize_template(self.NAME_URL, context)
    if val is None:
      val = self._url
    return val
  
  url = property(get_url,set_url,None,'URL param for Request')
  NAME_HEADERS = 'headers'

  def set_headers(self, value, isTemplate=False):
    """ Set headers, passing flag if using a template """
    if isTemplate:
      self.set_template(self.NAME_HEADERS, 'Dict_Templated')
    else:
      self.del_template(self.NAME_HEADERS)
    self._headers = value

  def get_headers(self, context=None):
    """ Get headers, applying template if pertintent """
    if not context or not self.templates or self.NAME_HEADERS not in self.templates:
      return self._headers

    vals = context.get_values()

    def template_tuple(tuple_input):
      return (string.Template(str(tuple_item)).safe_substitute(vals) for tuple_item in tuple_input)
    return dict(map(template_tuple, self._headers.items()))

  headers = property(get_headers, set_headers, None,
            'Headers dictionary for request')

  def update_context_before(self,context):
    """ Make pre-test context updates, by applying variable and generator updates """
    if self.variable_binds:
      context.bind_variables(self.variable_binds)
    if self.generator_binds:
      for key,value in self.generator_binds.items():
        context.bind_generator_next(key,value)

  def update_context_after(self, response_body, headers, context):
    """ Run the extraction routines to update variables based on HTTP response body """
    if self.extract_binds:
      for key, value in self.extract_binds.items():
          result = value.extract(
            body=response_body, headers=headers, context=context)
          context.bind_variable(key, result)

  def is_context_modifier(self):
    """ Returns true if context can be modified by this test
    (disallows caching of templated test bodies) """
    return self.variable_binds or self.generator_binds or self.extract_binds

  def is_dynamic(self):
    """ Returns true if this test does templating """
    if self.templates:
      return True
    elif isinstance(self._body, ContentHandler) and self._body.is_dynamic():
      return True
    return False

  def realize(self, context=None):
    """ Return a fully-templated test object, for configuring curl
    Warning: this is a SHALLOW copy, mutation of fields will cause problems!
    Can accept a None context """
    if not self.is_dynamic() or context is None:
      return self
    else:
      selfcopy = self.silent_copy()
      selfcopy.templates = None
      if isinstance(self._body, ContentHandler):
        selfcopy._body = self._body.get_content(context)
      selfcopy._url = self.get_url(context=context)
      selfcopy._headers = self.get_headers(context=context)
      return selfcopy

  def realize_partial(self, context=None):
    """ Attempt to template out what is static if possible, and load files.
    Used for performance optimization, in cases where a test is re-run repeatedly
    WITH THE SAME Context.
    """

    if self.is_context_modifier():
    # Don't template what is changing
      return self
    elif self.is_dynamic():  # Dynamic but doesn't modify context, template everything
      return self.realize(context=context)

    # See if body can be replaced
    bod = self._body
    newbod = None
    if bod and isinstance(bod, ContentHandler) and bod.is_file and not bod.is_template_path:
      # File can be UN-lazy loaded
      newbod = bod.create_noread_version()

    output = self
    if newbod:  # Read body
      output = copy.copy(self)
      output._body = newbod
    return output

  def __init__(self):
    self.headers = dict()
    self.expected_status = [200]
    self.templated = dict()

  def __str__(self):
    return json.dumps(self, default=parsing.safe_to_json)


  def configure_request(self,timeout=DEFAULT_TIMEOUT,context=None,request_handler=None):

    if request_handler:
      request = request_handler
    else:
      request = requests.Request()

    ## URL, TIMEOUT, METHOD
    request.url = self.url
    request.timeout = timeout
    request.method = self.method

    d_body = self.body
    #BODY, AUTH, JSON
    if d_body and len(d_body) >  0:
      if self.auth_username and self.auth_password:
        auth = HTTPBasicAuth(self.auth_username,self.auth_password)
        request.auth = auth
      if 'Content-Type' in self.headers and 'json' in self.headers['Content-Type']:
        request.json = json.loads(d_body)
      else:
        request.body = d_body
    
    #Params
    if self.params:
      params = convert(self.params)
      request.params = params
    
    head = self.get_headers(context=context)
    head = copy.copy(head)

    if head:
      request.headers = head

    return request

  @classmethod	
  def parse_test(cls,base_url,node,input_test=None,test_path=None):

    mytest = input_test
    if not mytest:
      mytest = Test()
    
    node = parsing.flatten_dictionaries(node)['data']
    node = parsing.lowercase_keys(node)
    node = node['data']
    CONFIG_ELEMENTS = {
      u'method' : [coerce_http_method],
      u'scope' : [coerce_to_string],
      u'name'	: [coerce_to_string],
      u'expected_status' : [coerce_list_of_ints],
      u'delay' : [lambda x:int(x)],
      u'group': [coerce_to_string],
      u'stop_on_failure' : [parsing.safe_to_bool],

      u'body': [ContentHandler.parse_content]
    }

    def use_config_parser(configobject, configelement, configvalue):
      
      myparsing = CONFIG_ELEMENTS.get(configelement)
      if myparsing:
        converted = myparsing[0](configvalue)
        setattr(configobject,configelement,converted)
        return True
      return False

    for configelement, configvalue in node.items():
      if use_config_parser(mytest, configelement, configvalue):
        continue

      if configelement == u'endpoint':
        if mytest.local_url:
          url_val = mytest.local_url
        else:
          url_val = base_url

        if isinstance (configvalue,dict):
          val = parsing.lowercase_keys(configvalue)
          val = val['data'][u'template']
          assert isinstance(val,str) or isinstance(val,int)
          url = urljoin(url_val,coerce_to_string(val))
          mytest.set_url(url, isTemplate=True)
        else:
          assert isinstance(configvalue,str) or isinstance(configvalue, int)
          mytest.url = urljoin(url_val, coerce_to_string(configvalue))
      
      if configelement == u'url':
        if isinstance (configvalue,dict):
          val = parsing.lowercase_keys(configvalue)['data']
          val = val[u'template']
          assert isinstance(val,str) or isinstance(val,int)
          mytests.local_url = str(val)
        else:
          assert isinstance(configvalue,str) or isinstance(configvale,int)
          mytest.local_url = str(configvalue)
      
      elif configelement == u'extract_binds':				
        binds = parsing.flatten_dictionaries(configvalue)
        binds = binds['data']
        if mytest.extract_binds is None:
          mytest.extract_binds = dict()

        for variable_name, extractor in binds.items():
          if not isinstance(extractor,dict) or len(extractor) == 0:
            raise TypeError(
              """Extractors must be defined as maps of extractorType:{configs} with 1 entry.""")
          if len(extractor) > 1:
            raise ValueError(
              """Cannot define multiple extractors for given variable name""")

          for extractor_type, extractor_config in extractor.items():
            mytest.extract_binds[variable_name] = validators.parse_extractor(extractor_type, extractor_config)

      elif configelement == u'validators':
        if not isinstance(configvalue,list):
          raise Exception(
            'Misconfigured validator section, must be a list of validators')
        if mytest.validators is None:
          mytest.validators = list()

        for var in configvalue:
          if not isinstance(var,dict):
            raise TypeError(
              "Validators must be defined as ValidatorType:{configs}")
          for validator_type, validator_config in var.items():
            validator = validators.parse_validator(validator_type,validator_config)
            mytest.validators.append(validator)
    
      elif configelement == 'headers':
        mytest.headers
        configvalue = parsing.flatten_dictionaries(configvalue)
        configvalue = configvalue['data']

        if isinstance(configvalue, dict):
          filterfunc = lambda x:str(x[0]).lower() == 'template'
          templates = [x for x in filter(filterfunc,configvalue.items())]
        else:
          templates = None

        if templates:
          mytest.set_headers(templates[0][1], isTemplate=True)
        elif isinstance(configvalue, dict):
          mytest.headers = configvalue
        else:
          raise TypeError("Illegal header type: headers must be a dictionary or list of dictionary keys")
      
      elif configelement == 'variable_binds':
        mytest.variable_binds = (parsing.flatten_dictionaries(configvalue))['data']

      
      elif configelement == 'generator_binds':
        output = flatten_dictionaries(configvalue)
        output = output['data']
        output2 = dict()
        for key,value in output.items():
          output2[str(key)] = str(value)
        mytest.generator_binds = output2

    if 'expected_status' not in node.keys():
      mytest.expected_status = [200,201,204]
    
    return mytest