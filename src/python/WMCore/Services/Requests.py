#!/usr/bin/python
"""
_Requests_

A set of classes to handle making http and https requests to a remote server and
deserialising the response.
"""

__revision__ = "$Id: Requests.py,v 1.33 2010/01/27 19:44:35 meloam Exp $"
__version__ = "$Revision: 1.33 $"

import urllib
import os
import sys
import base64
from httplib import HTTPConnection
from httplib import HTTPSConnection
from WMCore.WMException import WMException
from WMCore.Wrappers import JsonWrapper as json
from WMCore.Wrappers.JsonWrapper import JSONEncoder, JSONDecoder
import types
import pprint

class Requests(dict):
    """
    Generic class for sending different types of HTTP Request to a given URL
    """

    def __init__(self, url = 'localhost', dict={}):
        """
        url should really be host - TODO fix that when have sufficient code 
        coverage
        """
        #set up defaults
        self.setdefault("accept_type", 'text/html')
        self.setdefault("content_type", 'application/x-www-form-urlencoded')
        self.setdefault("host", url)
        
        # then update with the incoming dict
        self.update(dict)
        
        # and then get the URL opener
        self.setdefault("conn", self._getURLOpener())
        self.additionalHeaders = {}
        return

    def get(self, uri=None, data={}, encode = True, decode=True, contentType=None):
        """
        GET some data
        """
        return self.makeRequest(uri, data, 'GET', encode, decode, contentType)

    def post(self, uri=None, data={}, encode = True, decode=True, contentType=None):
        """
        POST some data
        """
        return self.makeRequest(uri, data, 'POST', encode, decode, contentType)

    def put(self, uri=None, data={}, encode = True, decode=True, contentType=None):
        """
        PUT some data
        """
        return self.makeRequest(uri, data, 'PUT', encode, decode, contentType)
       
    def delete(self, uri=None, data={}, encode = True, decode=True, contentType=None):
        """
        DELETE some data
        """
        return self.makeRequest(uri, data, 'DELETE', encode, decode, contentType)

    def makeRequest(self, uri=None, data={}, verb='GET',
                     encoder=True, decoder=True, contentType=None):
        """
        Make a request to the remote database. for a give URI. The type of
        request will determine the action take by the server (be careful with
        DELETE!). Data should be a dictionary of {dataname: datavalue}.
        
        Returns a tuple of the data from the server, decoded using the 
        appropriate method the response status and the response reason, to be 
        used in error handling. 
        
        You can override the method to encode/decode your data by passing in an 
        encoding/decoding function to this method. Your encoded data must end up 
        as a string.
        
        """
        # $client/$client_version (CMS) $http_lib/$http_lib_version $os/$os_version ($arch)
        if contentType:
            headers = {"Content-type": contentType,
                   "User-agent": "WMCore.Services.Requests/v001",
                   "Accept": self['accept_type']}
        else:
            headers = {"Content-type": self['content_type'],
                   "User-agent": "WMCore.Services.Requests/v001",
                   "Accept": self['accept_type']}
        encoded_data = ''
        
        for key in self.additionalHeaders.keys():
            headers[key] = self.additionalHeaders[key]
        
        # If you're posting an attachment, the data might not be a dict
        #   please test against ConfigCache_t if you're unsure.
        #assert type(data) == type({}), \
        #        "makeRequest input data must be a dict (key/value pairs)"
        
        # There must be a better way to do this...
        def f(): pass
        
        if verb != 'GET' and data:
            if type(encoder) == type(self.get) or type(encoder) == type(f):
                encoded_data = encoder(data)
            elif encoder == False:
                # Don't encode the data more than we have to
                #  we don't want to URL encode the data blindly, 
                #  that breaks POSTing attachments... ConfigCache_t
                #encoded_data = urllib.urlencode(data)
                #  -- Andrew Melo 25/7/09
                encoded_data = data
            else:
                # Either the encoder is set to True or it's junk, so use 
                # self.encode
                encoded_data = self.encode(data)
            headers["Content-length"] = len(encoded_data)
        elif verb == 'GET' and data:
            #encode the data as a get string
            uri = "%s?%s" % (uri, urllib.urlencode(data, doseq=True))
            
        headers["Content-length"] = len(encoded_data)
        self['conn'].connect()
        assert type(encoded_data) == type('string'), \
                    "Data in makeRequest is %s and not encoded to a string" % type(encoded_data)
        
        self['conn'].request(verb, uri, encoded_data, headers)
        response = self['conn'].getresponse()
        data = response.read()
        self['conn'].close()
        
        if type(decoder) == type(self.makeRequest) or type(decoder) == type(f):
            data = decoder(data)
        elif decoder != False:
            data = self.decode(data)
        return data, response.status, response.reason

    def encode(self, data):
        """
        encode data into some appropriate format, for now make it a string...
        """
        return urllib.urlencode(data, doseq=1)

    def decode(self, data):
        """
        decode data to some appropriate format, for now make it a string...
        """
        return data.__str__()
    
    def _getURLOpener(self):
        """
        method getting an HTTPConnection, it is used by the constructor such 
        that a sub class can override it to have different type of connection
        i.e. - if it needs authentication, or some fancy handler 
        """
        return HTTPConnection(self['host'])

class _EmptyClass:
    pass


class JSONThunker:
    """
    _JSONThunker_
    Converts an arbitrary object to <-> from a jsonable object.
    
    Will, for the most part "do the right thing" about various instance objects
    by storing their class information along with their data in a dict. Handles
    a recursion limit to prevent infinite recursion.
    
    self.passThroughTypes - stores a list of types that should be passed
      through unchanged to the JSON parser
      
    self.blackListedModules - a list of modules that should not be stored in
      the JSON.
    
    """
    def __init__(self):
        self.passThroughTypes = (types.NoneType,
                                 types.BooleanType,
                                 types.IntType,
                                 types.LongType,
                                 types.ComplexType,
                                 types.StringTypes,
                                 types.StringType,
                                 types.UnicodeType
                                 )
        # objects that inherit from dict should be treated as a dict
        #   they don't store their data in __dict__. There was enough
        #   of those classes that it warrented making a special case
        self.dictSortOfObjects = ( ('WMCore.Datastructs.Job', 'Job'),
                                   ('WMCore.WMBS.Job', 'Job'),
                                   ('WMCore.Database.CMSCouch', 'Document' ))
        # ditto above, but for lists
        self.listSortOfObjects = ( ('WMCore.DataStructs.JobPackage', 'JobPackage' ),
                                   ('WMCore.WMBS.JobPackage', 'JobPackage' ),)
        
        self.foundIDs = {}
        # modules we don't want JSONed
        self.blackListedModules = ('sqlalchemy.engine.threadlocal',
                                   'WMCore.Database.DBCore',
                                   'logging',
                                   'WMCore.DAOFactory',
                                   'WMCore.WMFactory',
                                   'WMFactory',
                                   'WMCore.Configuration',
                                   'WMCore.Database.Transaction',
                                   'threading',
                                   'datetime')
        
    def checkRecursion(self, data):
        """
        handles checking for infinite recursion
        """
        if (id(data) in self.foundIDs):
            if (self.foundIDs[id(data)] > 5):
                self.unrecurse(data)
                return "**RECURSION**"
            else:
                self.foundIDs[id(data)] += 1
                return data
        else:
            self.foundIDs[id(data)] = 1
            return data
           
    def unrecurse(self, data):
        """
        backs off the recursion counter if we're returning from _thunk
        """
        self.foundIDs[id(data)] = self.foundIDs[id(data)] -1
         
    def checkBlackListed(self, data):
        """
        checks to see if a given object is from a blacklisted module
        """
        try:
            # special case
            if ((data.__class__.__module__ == 'WMCore.Database.CMSCouch') and
                (data.__class__.__name__ == 'Document')):
                data.__class__ = type({})
                return data
            if (data.__class__.__module__ in self.blackListedModules):
                return "Blacklisted JSON object: module %s, name %s, str() %s" %\
                    (data.__class__.__module__,data.__class__.__name__ , str(data))
            else:
                return data
        except:
            return data

    
    def thunk(self, toThunk):
        """
        Thunk - turns an arbitrary object into a JSONable object
        """
        self.foundIDs = {}
        data = self._thunk(toThunk)
        return data
    
    def unthunk(self, data):
        """
        unthunk - turns a previously 'thunked' object back into a python object
        """
        return self._unthunk(data)
    
    def handleSetThunk(self, toThunk):
        toThunk = self.checkRecursion( toThunk )
        tempDict = {'thunker_encoded_json':True, 'type': 'set'}
        tempDict['set'] = self._thunk(list(toThunk))
        self.unrecurse(toThunk)
        return tempDict
    
    def handleListThunk(self, toThunk):
        toThunk = self.checkRecursion( toThunk )
        for k,v in enumerate(toThunk):
                toThunk[k] = self._thunk(v)
        self.unrecurse(toThunk)
        return toThunk
    
    def handleDictThunk(self, toThunk):
        toThunk = self.checkRecursion( toThunk )
        special = False
        tmpdict = {}
        for k,v in toThunk.iteritems():
            if type(k) == type(int): 
                special = True
                tmpdict['_i:%s' % k] = self._thunk(v)
            elif type(k) == type(float): 
                special = True
                tmpdict['_f:%s' % k] = self._thunk(v)
            else:
                tmpdict[k] = self._thunk(v)
        if special:
            toThunk['thunker_encoded_json'] = self._thunk(True)
            toThunk['type'] = self._thunk('dict')
            toThunk['dict'] = tmpdict
        else:
            toThunk.update(tmpdict)
        self.unrecurse(toThunk)
        return toThunk
    
    def handleObjectThunk(self, toThunk):
        toThunk = self.checkRecursion( toThunk )
        toThunk = self.checkBlackListed(toThunk)
        
        if (type(toThunk) == type("")):
            # things that got blacklisted
            return toThunk
        if (hasattr(toThunk, '__to_json__')):
            #Use classes own json thunker
            toThunk2 = toThunk.__to_json__(self)
            self.unrecurse(toThunk)
            return toThunk2
        elif ( isinstance(toThunk, dict) ):
            toThunk2 = self.handleDictObjectThunk( toThunk )
            self.unrecurse(toThunk)
            return toThunk2
        elif ( isinstance(toThunk, list) ):
            #a mother thunking list
            toThunk2 = self.handleListObjectThunk( toThunk )
            self.unrecurse(toThunk)
            return toThunk2
        else:
            try:
                thunktype = '%s.%s' % (toThunk.__class__.__module__,
                                       toThunk.__class__.__name__)
                tempDict = {'thunker_encoded_json':True, 'type': thunktype}
                tempDict[thunktype] = self._thunk(toThunk.__dict__)
                self.unrecurse(toThunk)
                return tempDict
            except Exception, e:
                tempDict = {'json_thunk_exception_' : "%s" % e }
                self.unrecurse(toThunk)
                return tempDict
            
    def handleDictObjectThunk(self, data):
        thunktype = '%s.%s' % (data.__class__.__module__,
                               data.__class__.__name__)
        tempDict = {'thunker_encoded_json':True, 
                    'is_dict': True,
                    'type': thunktype, 
                    thunktype: {}}
        
        for k,v in data.__dict__.iteritems():
            tempDict[k] = self._thunk(v)
        for k,v in data.iteritems():
            tempDict[thunktype][k] = self._thunk(v)
            
        return tempDict
    
    def handleDictObjectUnThunk(self, value, data):
        data.pop('thunker_encoded_json', False)
        data.pop('is_dict', False)
        thunktype = data.pop('type', False)
        
        for k,v in data.iteritems():
            if (k == thunktype):
                for k2,v2 in data[thunktype].iteritems():
                    value[k2] = self._unthunk(v2)
            else:
                value.__dict__[k] = self._unthunk(v)
        return value
    
    def handleListObjectThunk(self, data):
        thunktype = '%s.%s' % (data.__class__.__module__,
                               data.__class__.__name__)
        tempDict = {'thunker_encoded_json':True, 
                    'is_list': True,
                    'type': thunktype, 
                    thunktype: []}
        for k,v in enumerate(data):
            tempDict['thunktype'].append(self._thunk(v)) 
        for k,v in data.__dict__.iteritems():
            tempDict[k] = self._thunk(v)           
        return tempDict
    
    def handleListObjectUnThunk(self, value, data):
        data.pop('thunker_encoded_json', False)
        data.pop('is_list', False)
        thunktype = data.pop('type')
        tmpdict = {}
        for k,v in data[thunktype].iteritems():
            setattr(value, k, self._unthunk(v))
            
        for k,v in data.iteritems():
            if (k == thunktype):
                continue
            value.__dict__ = self._unthunk(v)
        return value
    
    def _thunk(self, toThunk):
        """
        helper function for thunk, does the actual work
        """
        
        if (type(toThunk) in self.passThroughTypes):
            return toThunk
        elif (type(toThunk) == type([])):
            return self.handleListThunk(toThunk)
        
        elif (type(toThunk) == type({})):
            return self.handleDictThunk(toThunk)
        
        elif ((type(toThunk) == type(set()))):
            return self.handleSetThunk(toThunk)
        
        elif (type(toThunk) == types.FunctionType):
            self.unrecurse(toThunk)
            return "function reference"
        elif (isinstance(toThunk, object)):
            return self.handleObjectThunk(toThunk)
        else:
            self.unrecurse(toThunk)
            raise RuntimeError, type(toThunk)
        
    def _unthunk(self, jsondata):
        """
        _unthunk - does the actual work for unthunk
        """
        if (type(jsondata) == types.UnicodeType):
            return str(jsondata)
        if (type(jsondata) == type({})):
            if ('thunker_encoded_json' in jsondata):
                # we've got a live one...
                if jsondata['type'] == 'set':
                    newSet = set()
                    for i in self._unthunk(jsondata['set']):
                        newSet.add( self._unthunk( i ) )
                    return newSet
                if jsondata['type'] == 'dict':
                    # We have a "special" dict
                    data = {}
                    for k,v in jsondata['dict'].iteritems():
                        tmp = self._unthunk(v)
                        if k.startswith('_i:'):
                            data[int(k.lstrip('_i:'))] = tmp
                        elif k.startswith('_f:'):
                            data[float(k.lstrip('_f:'))] = tmp
                        else:
                            data[k] = tmp
                    return data
                else:
                    # spawn up an instance.. good luck
                    #   here be monsters
                    #   inspired from python's pickle code
                    ourClass = self.getThunkedClass(jsondata)
                    
                    value = _EmptyClass()
                    if (hasattr(ourClass, '__from_json__')):
                        # Use classes own json loader
                        try:
                            value.__class__ = ourClass
                        except:
                            value = ourClass()
                        value = ourClass.__from_json__(value, data, self)
                    elif ('thunker_encoded_json' in jsondata and
                                     'is_dict' in jsondata):
                        try:
                            value.__class__ = ourClass
                        except:
                            value = ourClass()
                        value = self.handleDictObjectUnThunk( value, jsondata )
                    elif ( 'thunker_encoded_json' in jsondata ):
                        #print "list obj unthunk"  
                        try:
                            value.__class__ = ourClass
                        except:
                            value = ourClass()
                        value = self.handleListObjectUnThunk( value, jsondata )
                    else:
                        #print "did we get here"
                        try:
                            value.__class__ = getattr(ourClass, name).__class__
                            #print "changed the class to %s " % value.__class__
                        except Exception, ex:
                            #print "Except1 in requests %s " % ex
                            try:
                                #value = _EmptyClass()
                                value.__class__ = ourClass
                            except Exception, ex2:
                                #print "Except2 in requests %s " % ex2
                                #print type(ourClass)
                                try:
                                    value = ourClass();
                                except:
                                    #print 'megafail'
                                    pass
                        
                        #print "name %s module %s" % (name, module)
                        value.__dict__ = data
                #print "our value is %s "% value
                return value
            else:
                #print 'last ditch attempt'
                data = {}
                for k,v in jsondata.iteritems():
                    data[k] = self._unthunk(v)
                return data
 
        else:
            return jsondata
        
    def getThunkedClass(self, jsondata):
        """
        Work out the class from it's thunked json representation
        """
        module = jsondata['type'].rsplit('.',1)[0]
        name = jsondata['type'].rsplit('.',1)[1]
        if (module == 'WMCore.Services.Requests') and (name == JSONThunker):
            raise RuntimeError, "Attempted to unthunk a JSONThunker.."
        
        __import__(module)
        mod = sys.modules[module]
        ourClass = getattr(mod, name)
        return ourClass
                   
class JSONRequests(Requests):
    """
    Example implementation of Requests that encodes data to/from JSON.
    """
    def __init__(self, url = 'localhost:8080'):
        Requests.__init__(self, url)
        self['accept_type'] = "application/json"
        self['content_type'] = "application/json"

    def encode(self, data):
        """
        encode data as json
        """
        encoder = JSONEncoder()
        thunker = JSONThunker()
        thunked = thunker.thunk(data)
        return encoder.encode(thunked)
    

    def decode(self, data):
        """
        decode the data to python from json
        """
        if data:
            decoder = JSONDecoder()
            thunker = JSONThunker()
            data =  decoder.decode(data)
            unthunked = thunker.unthunk(data)
            return unthunked
        else:
            return {}      

class BasicAuthJSONRequests(JSONRequests):
    """
    _BasicAuthJSONRequests_

    Support basic HTTP auth for JSON requests.  The username and password must
    be embedded into the url in the following form:
        username:password@hostname
    """
    def __init__(self, url = "localhost:8080"):
        if url.find("@") == -1:
            JSONRequests.__init__(self, url)
            return

        (auth, hostname) = url.split("@", 2)

        JSONRequests.__init__(self, hostname)
        self.additionalHeaders["Authorization"] = \
            "Basic " + base64.encodestring(auth).strip()

        return

class SSLRequests(Requests):
    """
    Implementation of Requests using HTTPS to send requests to a given URL, 
    without authenticating via a key/cert pair.
    """ 
    def _getURLOpener(self):
        """
        method getting a secure (HTTPS) connection
        """
        return HTTPSConnection(self['host'])

class SSLJSONRequests(JSONRequests):
    """
    _SSLJSONRequests_
    
    Implementation of JSONRequests using HTTPS to send requests to a given URL, 
    without authenticating via a key/cert pair.
    """ 
    def _getURLOpener(self):
        """
        _getURLOpener_
        
        Retrieve a secure (HTTPS) connection.
        """
        return HTTPSConnection(self["host"])    
    
class SecureRequests(Requests):
    """
    Implementation of Requests using a different connection type, e.g. use HTTPS
    to send requests to a given URL, authenticating via a key/cert pair
    """ 
    def _getURLOpener(self):
        """
        method getting a secure (HTTPS) connection
        """
        key, cert = self.getKeyCert()
        return HTTPSConnection(self['host'], key_file=key, cert_file=cert)
    
    def getKeyCert(self):
        """
       _getKeyCert_
       
       Gets the User Proxy if it exists, otherwise throws an exception.
       This code is borrowed from DBSAPI/dbsHttpService.py
        """
        # Zeroth case is if the class has over ridden the key/cert and has it
        # stored in self
        if self.has_key('cert') and self.has_key('key' ) \
             and self['cert'] and self['key']:
            key = self['key']
            cert = self['cert']
        # Now we're trying to guess what the right cert/key combo is... 
        # First presendence to HOST Certificate, This is how it set in Tier0
        elif os.environ.has_key('X509_HOST_CERT'):
            cert = os.environ['X509_HOST_CERT']
            key = os.environ['X509_HOST_KEY']
    
        # Second preference to User Proxy, very common
        elif (os.environ.has_key('X509_USER_PROXY')) and \
                (os.path.exists( os.environ['X509_USER_PROXY'])):
            cert = os.environ['X509_USER_PROXY']
            key = cert
    
        # Third preference to User Cert/Proxy combinition
        elif os.environ.has_key('X509_USER_CERT'):
            cert = os.environ['X509_USER_CERT']
            key = os.environ['X509_USER_KEY']
        
        # TODO: only in linux, unix case, add other os case
        # look for proxy at default location /tmp/x509up_u$uid
        elif os.path.exists('/tmp/x509up_u'+str(os.getuid())):
            cert = '/tmp/x509up_u'+str(os.getuid())
            key = cert
            
        # Worst case, hope the user has a cert in ~/.globus
        else :
            cert = os.environ['HOME'] + '/.globus/usercert.pem'
            if os.path.exists(os.environ['HOME'] + '/.globus/userkey.pem'):
                key = os.environ['HOME'] + '/.globus/userkey.pem'
            else:
                key = cert
    
        #Set but not found
        if not os.path.exists(cert) or not os.path.exists(key):
            raise WMException('Request requires a host certificate and key', 
                              "WMCORE-11")
  
        # All looks OK, still doesn't guarantee proxy's validity etc.
        return key, cert
