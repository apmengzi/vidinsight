Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "D:\CyberStomach\VidInsight\backend"
sh.Run "D:\CyberStomach\VidInsight\.venv\Scripts\pythonw.exe scripts\miku_checkin_daily.py", 0, False
