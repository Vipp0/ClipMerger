"""Lettura del testo dalle immagini con il lettore integrato in Windows (Windows.Media.Ocr), in italiano.
Non richiede installazioni: si usa PowerShell, che sul PC c'è sempre. La lingua usata è quella di
Windows (serve l'italiano tra le lingue installate: di solito lo è)."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import applog
from utils import CREATE_NO_WINDOW

# Letto una volta per cartella di immagini; scrive «ocr.json» (nome file senza estensione -> righe di testo).
_SCRIPT = r"""
param([string]$Dir)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
[void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime]
[void][Windows.Storage.StorageFile, Windows.Foundation, ContentType=WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) {
    $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $t.Wait(-1) | Out-Null
    $t.Result
}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if (-not $engine) { throw 'nessun lettore di testo disponibile in Windows' }
$res = [ordered]@{}
foreach ($f in Get-ChildItem $Dir -Filter *.png | Sort-Object Name) {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($f.FullName)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bmp = Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $r = Await ($engine.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
    $res[$f.BaseName] = @($r.Lines | ForEach-Object { $_.Text })
    $stream.Dispose()
}
[System.IO.File]::WriteAllText((Join-Path $Dir 'ocr.json'), ($res | ConvertTo-Json -Depth 4), (New-Object System.Text.UTF8Encoding($false)))
"""


class OcrError(Exception):
    pass


def read_folder(folder: Path, timeout: float = 300.0) -> dict[str, list[str]]:
    """Reads every .png in `folder`; returns {file name without extension: [text lines]}."""
    script = Path(tempfile.gettempdir()) / "ClipMerger_ocr.ps1"
    script.write_text(_SCRIPT, encoding="utf-8-sig")  # BOM: PowerShell 5 reads it as UTF-8
    out = folder / "ocr.json"
    out.unlink(missing_ok=True)
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
             "-Dir", str(folder)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise OcrError(f"Il lettore di testo di Windows non è partito: {exc}") from exc
    if proc.returncode != 0 or not out.is_file():
        applog.log.error("OCR fallito (codice %s): %s", proc.returncode, proc.stderr.strip()[-800:])
        raise OcrError("Il lettore di testo di Windows non ha funzionato (serve l'italiano tra le lingue di Windows).")
    data = json.loads(out.read_text(encoding="utf-8-sig") or "{}")
    # PowerShell writes a single line as a plain string, no line as an empty object
    return {k: ([v] if isinstance(v, str) else list(v) if isinstance(v, list) else []) for k, v in data.items()}
