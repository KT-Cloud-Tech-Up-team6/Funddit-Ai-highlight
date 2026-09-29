# -*- coding: utf-8 -*-
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
os.environ["LOG_FORMAT"] = "text"
from api import settings; settings.ensure_runtime_env()
from api import jobs
jobs.run_render("9164ad22666d", ["seg_1"], "crop", False, 0.5)
j = jobs.get("9164ad22666d") or {}
print("\n상태:", j.get("status"), "| 쇼츠:", j.get("short_count"), "| 소요:", j.get("elapsed_sec"), "초")
if j.get("error"): print("오류:", j["error"])
