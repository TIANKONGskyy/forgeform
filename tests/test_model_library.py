# -*- coding: utf-8 -*-
"""模型库路由回归：列表 / 预览 / 文件 / 下载 / 发布鉴权。

2026-10-19：`/model-library` 原来只是个 "Under construction" 占位；
backend/model_library.py 其实早就写完了，只是没挂到 FastAPI。
这里用临时库目录 + TestClient 驱动真实路由，不碰生产 outputs/model_library.db。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.model_library as ml


class ModelLibraryRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls._old_db, cls._old_dir = ml.LIB_DB, ml.LIB_DIR
        ml.LIB_DB = Path(cls.tmp.name) / "model_library.db"
        ml.LIB_DIR = Path(cls.tmp.name) / "files"
        ml.LIB_DIR.mkdir(parents=True, exist_ok=True)
        ml.init_db()

        # token 必须在注册路由之前设好 —— register_model_library() 里只读一次
        os.environ["DSH_OPS_TOKEN"] = "test-token"
        from app import register_model_library

        app = FastAPI()
        register_model_library(app)
        cls.client = TestClient(app)

        # 源文件必须放在真实的 outputs/ 下 —— publish() 现在会校验路径白名单，
        # 拿临时目录里的文件会被拒（这是刻意的：不许把系统文件发布成公开下载）。
        cls.src = Path(__file__).resolve().parent.parent / "outputs" / "_test_phone-stand.stl"
        cls.src.parent.mkdir(parents=True, exist_ok=True)
        cls.src.write_bytes(b"solid stand\nendsolid stand\n")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.src.unlink(missing_ok=True)
        except Exception:
            pass
        os.environ.pop("DSH_OPS_TOKEN", None)
        ml.LIB_DB, ml.LIB_DIR = cls._old_db, cls._old_dir
        cls.tmp.cleanup()

    def test_01_publish_requires_token(self):
        r = self.client.post("/model-library/publish", json={"file_path": str(self.src)})
        self.assertEqual(r.status_code, 403)
        r = self.client.post(
            "/model-library/publish",
            json={"file_path": str(self.src)},
            headers={"X-Ops-Token": "wrong"},
        )
        self.assertEqual(r.status_code, 403)

    def test_02_publish_and_browse(self):
        r = self.client.post(
            "/model-library/publish",
            json={"file_path": str(self.src), "name": "手机支架", "tags": "stand,test"},
            headers={"X-Ops-Token": "test-token"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        mid = r.json()["id"]
        self.assertTrue(mid)

        page = self.client.get("/model-library")
        self.assertEqual(page.status_code, 200)
        self.assertIn("模型库", page.text)
        self.assertIn("手机支架", page.text)

        preview = self.client.get(f"/model-library/preview/{mid}")
        self.assertEqual(preview.status_code, 200)
        self.assertIn("手机支架", preview.text)

        raw = self.client.get(f"/model-library/file/{mid}")
        self.assertEqual(raw.status_code, 200)
        self.assertEqual(raw.content, self.src.read_bytes())

        dl = self.client.get(f"/model-library/download/{mid}")
        self.assertEqual(dl.status_code, 200)
        self.assertIn("attachment", dl.headers.get("content-disposition", ""))

        self.assertEqual(ml.get_model(mid)["view_count"], 1)
        self.assertEqual(ml.get_model(mid)["dl_count"], 1)

        # 缺失 id 一律 404，不是 500
        self.assertEqual(self.client.get("/model-library/preview/nope").status_code, 404)
        self.assertEqual(self.client.get("/model-library/file/nope").status_code, 404)

        # 删除也要 token
        self.assertEqual(self.client.delete(f"/model-library/{mid}").status_code, 403)
        self.assertEqual(
            self.client.delete(f"/model-library/{mid}", headers={"X-Ops-Token": "test-token"}).status_code,
            200,
        )
        self.assertIsNone(ml.get_model(mid))


if __name__ == "__main__":
    unittest.main()
