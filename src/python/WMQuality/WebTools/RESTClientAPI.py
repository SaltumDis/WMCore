import urllib
from urlparse import urlparse
from httplib import HTTPConnection
from WMCore.WebTools.Page import make_rfc_timestamp
from WMCore.Wrappers import JsonWrapper
    
def makeRequest(url, values=None, type='GET', accept="text/plain", 
                contentType = None):
    headers = {}
    contentType = contentType or "application/x-www-form-urlencoded"
    headers = {"Content-type": contentType,
               "Accept": accept}
    data = None
    if type == 'GET' and values:
        data = urllib.urlencode(values)
    elif type != 'GET' and values:
        # needs to test other encoding type
        if contentType == "application/x-www-form-urlencoded":
            data = urllib.urlencode(values)
        else:
            # for other encoding scheme values assumed to be encoded already
            data = values
    parser = urlparse(url)
    uri = parser.path
    if parser.query:
        uri += "?" + parser.query
        
    if type != 'POST' and data != None:
        uri = '%s?%s' % (uri, data)
    conn = HTTPConnection(parser.netloc)
    conn.connect()
    conn.request(type, uri, data, headers)
    response = conn.getresponse()
    
    data = response.read()
    conn.close()
    type = response.getheader('content-type').split(';')[0]
    return data, response.status, type, response

def methodTest(verb, url, input={}, accept='text/json', contentType = None, output={} , expireTime=300):
    
    data, code, type, response = makeRequest(url, input, verb, accept, contentType)
    
    keyMap = {'code': code, 'data': data, 'type': type, 'response': response}
    for key, value in output.items():
        assert keyMap[key] == value, \
            'Got a return %s != %s (got %s) (data %s)' % (key, value, keyMap[key], data)
    
    expires = response.getheader('Expires')        
    assert expires == make_rfc_timestamp(expireTime), 'Expires header incorrect (%s)' % expires
    
    return data, expires