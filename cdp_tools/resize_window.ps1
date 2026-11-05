param([int]$W, [int]$H)
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32 {
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr after, int x, int y, int cx, int cy, uint flags);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr hWnd, int x, int y, int w, int h, bool repaint);
  public struct RECT { public int L, T, R, B; }
}
"@
$p = Get-Process -Name vidinsight -ErrorAction Stop
$h = $p.MainWindowHandle
$r = New-Object Win32+RECT
[Win32]::GetWindowRect($h, [ref]$r) | Out-Null
# SWP_NOMOVE(2)|SWP_NOZORDER(4)|SWP_NOACTIVATE(0x10)
[Win32]::SetWindowPos($h, [IntPtr]::Zero, 0, 0, $W, $H, 2 -bor 4 -bor 0x10) | Out-Null
Start-Sleep -Milliseconds 900
"window set to ${W}x${H} (was $($r.R-$r.L)x$($r.B-$r.T))"
