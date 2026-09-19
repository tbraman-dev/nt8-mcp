# Screenshot a window by title substring (or the whole screen) to a PNG so Claude can read it.
# Usage: powershell -ExecutionPolicy Bypass -File shot.ps1 -Title "OF DOM" -Out C:\path\shot.png     (no -Title = full screen)
param([string]$Title = "", [string]$Out = "$env:TEMP\ntshot.png")
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System; using System.Runtime.InteropServices; using System.Text; using System.Collections.Generic;
public class W {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumDel cb, IntPtr l);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out R r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  public delegate bool EnumDel(IntPtr h, IntPtr l);
  [StructLayout(LayoutKind.Sequential)] public struct R { public int L, T, Rt, B; }
  public static List<KeyValuePair<IntPtr,string>> All() {
    var list = new List<KeyValuePair<IntPtr,string>>();
    EnumWindows((h, l) => { if (!IsWindowVisible(h)) return true; var sb = new StringBuilder(256); GetWindowText(h, sb, 256); if (sb.Length > 0) list.Add(new KeyValuePair<IntPtr,string>(h, sb.ToString())); return true; }, IntPtr.Zero);
    return list;
  }
}
"@
[W]::SetProcessDPIAware() | Out-Null
$bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
if ($Title -ne "") {
  $w = [W]::All() | Where-Object { $_.Value -like "*$Title*" } | Select-Object -First 1
  if (-not $w) { Write-Output "NO WINDOW matching '$Title'. Open windows:"; [W]::All() | ForEach-Object { $_.Value }; exit 1 }
  [W]::ShowWindow($w.Key, 9) | Out-Null  # SW_RESTORE, in case it's minimized
  [W]::SetForegroundWindow($w.Key) | Out-Null; Start-Sleep -Milliseconds 300
  $r = New-Object W+R; [W]::GetWindowRect($w.Key, [ref]$r) | Out-Null
  $bounds = New-Object System.Drawing.Rectangle($r.L, $r.T, ($r.Rt - $r.L), ($r.B - $r.T))
  Write-Output "window: $($w.Value)"
}
$bmp = New-Object System.Drawing.Bitmap($bounds.Width, $bounds.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($bounds.X, $bounds.Y, 0, 0, $bounds.Size)
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
Write-Output "saved $Out $($bounds.Width)x$($bounds.Height)"
