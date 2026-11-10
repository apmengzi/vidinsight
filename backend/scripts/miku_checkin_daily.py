# -*- coding: utf-8 -*-
"""miku 每日自动签到（供 Windows 计划任务经 VBS 以 pythonw 拉起，不闪窗）。

行为：lucky 模式（期望值高），失败换验证码最多 4 次；「今日已签到」视为成功跳过。
日志追加到 E:\\miku_checkin\\checkin.log（磁盘纪律：日志不进 C 盘）。
"""
import sys, os, datetime

# 计划任务环境里系统代理可能指向会破坏 miku TLS 的 7895 中转，直连才稳。
os.environ["NO_PROXY"] = (os.environ.get("NO_PROXY", "") + ",miku.app.itstudio.club").lstrip(",")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

LOG = r"E:\miku_checkin\checkin.log"


def log(msg: str) -> None:
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now():%F %T} {msg}\n")


def main() -> None:
    try:
        from app.services import miku_image as m
    except Exception as e:  # noqa: BLE001
        log(f"import 失败: {e!r}")
        return
    if not m.is_configured():
        log("未配置 miku 渠道（config/miku.json 缺失）")
        return
    bal_before = m.credit_balance()
    try:
        out = m.checkin("lucky")
        log(f"签到成功 credits=+{out.get('credits')} balance {bal_before} -> {out.get('balance')}")
    except m.MikuError as e:
        s = str(e)
        if "已签到" in s or "今日已" in s:
            log(f"跳过：今日已签到（余额 {bal_before}）")
        else:
            log(f"签到失败（4 张验证码均未过）: {s[:200]} 余额 {bal_before}")


if __name__ == "__main__":
    main()
