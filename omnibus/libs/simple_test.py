from omnibus.libs.content_parser import *
import os
import requests
from omnibus.libs.binding import Context
from omnibus.libs import util


base_url = 'http://127.0.0.1:6968/'
test_file = ['test_rest.yml','test_copy.yml']
test_structure = list()
paths = list()
for i in test_file:
    test_structure.append(util.load_yaml(i))
    paths.append(os.path.dirname(i))

for t,p,f in zip(test_structure,paths,test_file):
    var = {"mock_zone": 'test.com'}
    tests = parse_file(t,f,p,vars=var,global_url=base_url)
    res = run_testsets(tests)