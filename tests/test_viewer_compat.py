import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.viewer_compat import register_viewer_compat, CHOICE

class ViewerCompatTest(unittest.TestCase):
 def test_bundled_viewer_is_served_with_webgl(self):
  app=FastAPI();register_viewer_compat(app)
  routes=[r.path for r in app.routes if r.path.startswith('/assets/')]
  self.assertEqual(len(routes),1)
  response=TestClient(app).get(routes[0])
  self.assertEqual(response.status_code,200)
  self.assertNotIn(CHOICE,response.text)
  self.assertIn('function ad(){return"WebGL"}',response.text)
  self.assertEqual(response.headers['cache-control'],'no-cache')

if __name__=='__main__':unittest.main()
