"""Exercise the real production launcher against an isolated library, without a browser."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import urllib.error

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None

class PackagedAppTests(unittest.TestCase):
    def test_same_origin_ui_deep_link_api_and_collection_create(self):
        self.check_app('')

    def test_jupyter_prefix(self):
        self.check_app('/user/test/proxy/8000')

    def check_app(self, prefix):
        root=Path(os.getenv('COLLECTION_RELEASE_TEST_ROOT', str(Path(__file__).resolve().parents[2])))
        if not (root/'frontend/dist/index.html').exists(): self.skipTest('Build frontend before packaged UI test')
        with tempfile.TemporaryDirectory() as temp:
            library=Path(temp)/'library'
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
            env={k:v for k,v in os.environ.items() if not k.startswith('ARTIST_')}
            with (Path(temp)/'server.log').open('w+') as output:
                process=subprocess.Popen([sys.executable,str(root/'studio.py'),'run','--library',str(library),'--port',str(port),'--no-browser','--base-path',prefix],cwd=root,env=env,stdout=output,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                base=f'http://127.0.0.1:{port}'
                try:
                    ready=False
                    for _ in range(120):
                        if process.poll() is not None: break
                        try:
                            with urllib.request.urlopen(base+'/api/health',timeout=1) as response: ready=response.status==200
                            if ready: break
                        except OSError: time.sleep(.1)
                    if not ready:
                        output.seek(0);self.fail(output.read())
                    with urllib.request.urlopen(base+'/logs') as response:
                        html=response.read().decode()
                        self.assertIn('Illustration Scrapping Studio',html)
                        self.assertIn('<base href="'+prefix+'/">',html)
                        import re
                        asset=re.search(r'src="\./(assets/[^"]+)"',html).group(1)
                    with urllib.request.urlopen(base+'/'+asset) as response:self.assertEqual(response.status,200)
                    # A real Jupyter proxy strips the prefix before forwarding; inspect redirects without following locally.
                    opener=urllib.request.build_opener(NoRedirect())
                    with self.assertRaises(urllib.error.HTTPError) as redirect: opener.open(base+'/api/folders')
                    self.assertEqual(redirect.exception.code,307)
                    self.assertIn(prefix+'/api/folders/',redirect.exception.headers['Location'])
                    with urllib.request.urlopen(base+'/api/folders/') as response:self.assertEqual(json.load(response),[])
                    with urllib.request.urlopen(base+'/api/settings/processing') as response:
                        policy=json.load(response)
                        self.assertTrue(policy['enabled'])
                        self.assertEqual(policy['max_dimension'],2000)
                    policy.update(enabled=False, max_dimension=3072)
                    request=urllib.request.Request(base+'/api/settings/processing',data=json.dumps(policy).encode(),headers={'Content-Type':'application/json'},method='PUT')
                    with urllib.request.urlopen(request) as response:self.assertFalse(json.load(response)['enabled'])
                    with urllib.request.urlopen(base+'/api/settings/processing') as response:
                        saved=json.load(response)
                        self.assertFalse(saved['enabled'])
                        self.assertEqual(saved['max_dimension'],3072)

                    body=json.dumps({'name':'Landscape','type':'tag','query':'landscape sunset','sources':['danbooru']}).encode()
                    request=urllib.request.Request(base+'/api/folders/',data=body,headers={'Content-Type':'application/json'})
                    with urllib.request.urlopen(request) as response:
                        created=json.load(response);self.assertEqual(created['type'],'tag');self.assertIsNone(created['artist_tag_template'])
                    with self.assertRaises(urllib.error.HTTPError) as failure:urllib.request.urlopen(base+'/api/not-a-route')
                    self.assertEqual(failure.exception.code,404)
                    with urllib.request.urlopen(base+'/api/diagnostics') as response:self.assertTrue(json.load(response)['items'])
                finally:
                    process.terminate();process.wait(timeout=20)
