#!/usr/bin/env python
import os
import sys
import time
import subprocess

# 等待原进程结束
time.sleep(2)

# 重启主程序
cmd = ["C:\Program Files\Python311\python.exe", "G:\allbot\849allbot\新备份！\allbot\main.py"]
print("执行重启命令:", " ".join(cmd))
subprocess.Popen(cmd, cwd="G:\allbot\849allbot\新备份！\allbot", shell=False)

# 删除自身
try:
    os.remove(__file__)
except:
    pass
