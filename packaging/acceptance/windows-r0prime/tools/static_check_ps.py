#!/usr/bin/env python3
"""Off-Windows static check for the PowerShell files of this kit (a lint, NOT a parser: pwsh was not available).

Checks, per file:
  * ASCII only (Windows PowerShell 5.1 reads a BOM-less file as the ANSI code page)
  * balanced () [] {} outside comments/strings, with here-strings, '...' / "..." strings (incl. $() inside "..."), <# #> and # comments
  * no use of commands that change system/security state (execution policy, Defender, firewall rules, trust store, registry writes, bcdedit, netsh ...)
  * every Verb-Noun command used is either a known cmdlet or a function defined in the kit (typo finder)
Usage: python3 -I static_check_ps.py FILE.ps1 [...]   -> exit 0 if every file is clean, 1 otherwise.
"""
import re
import sys

FORBIDDEN = [
    r"Set-ExecutionPolicy", r"Set-MpPreference", r"Add-MpPreference", r"Remove-MpPreference", r"Disable-\w+", r"Enable-\w+",
    r"New-NetFirewallRule", r"Set-NetFirewall\w*", r"Remove-NetFirewall\w*", r"Set-NetConnectionProfile", r"netsh", r"\breg(?:\.exe)?\s+(?:add|delete|import)",
    r"bcdedit", r"Set-ItemProperty", r"New-ItemProperty", r"Remove-ItemProperty", r"Import-Certificate", r"Import-PfxCertificate", r"-addstore",
    r"Unblock-File", r"Install-\w+", r"Uninstall-\w+", r"Set-Service", r"Stop-Service", r"Start-Service", r"sc\.exe", r"schtasks", r"Register-ScheduledTask",
    r"Add-Type\s+-AssemblyName\s+System\.Management\.Automation", r"Invoke-Expression", r"\biex\b", r"DownloadString", r"DownloadFile", r"Invoke-WebRequest", r"Invoke-RestMethod",
    r"Set-Content\s+-Path\s+HKLM", r"New-Item\s+-Path\s+HKLM",
]
REVIEW = [r"Remove-Item", r"Stop-Process", r"taskkill", r"Start-Process", r"msiexec", r"Copy-Item", r"Out-File", r"Add-Content", r"WriteAllText", r"WriteAllBytes"]

KNOWN = set("""Add-Content Add-Member Add-Type Copy-Item ConvertFrom-Json ConvertTo-Json ForEach-Object Get-ChildItem Get-CimInstance Get-Command Get-Content Get-Culture
Get-Date Get-FileHash Get-Item Get-ItemProperty Get-MpComputerStatus Get-NetFirewallRule Get-NetTCPConnection Get-Process Get-Random Get-UICulture Get-WinEvent Get-AuthenticodeSignature
Format-Table New-Item New-Object Out-File Out-Null Out-String Read-Host Remove-Item Resolve-Path Select-Object Select-String Sort-Object Split-Path Start-Process Start-Sleep
Start-Transcript Stop-Process Stop-Transcript Test-Path Where-Object Write-Error Write-Host Write-Output Write-Warning Get-Member Join-Path Get-Variable""".split())

def lex(text):
    """returns (code_text, problems). code_text has strings/comments blanked (newlines kept)."""
    probs = []
    out = []
    i, n, line = 0, len(text), 1
    stack = []          # (char, line)
    modes = ["code"]    # code | dq
    pairs = {")": "(", "]": "[", "}": "{"}
    def err(msg): probs.append("line %d: %s" % (line, msg))
    while i < n:
        c = text[i]
        mode = modes[-1]
        if c == "\n": line += 1
        if mode == "dq":
            if c == "`":
                i += 2; continue
            if c == '"':
                if text[i + 1:i + 2] == '"': i += 2; continue
                modes.pop(); out.append(" "); i += 1; continue
            if c == "$" and text[i + 1:i + 2] == "(":
                modes.append("code"); stack.append(("$(", line)); i += 2; continue
            if c == "\n": out.append("\n")
            i += 1; continue
        # code mode
        if text.startswith("<#", i):
            j = text.find("#>", i + 2)
            if j < 0: err("unterminated <# comment"); break
            line += text.count("\n", i, j); out.append("\n" * text.count("\n", i, j)); i = j + 2; continue
        if c == "#" and (i == 0 or text[i - 1] in " \t\n;{(|"):
            j = text.find("\n", i); j = n if j < 0 else j; i = j; continue
        if text.startswith("@'", i) or text.startswith('@"', i):
            q = text[i + 1]
            m = re.compile(r"\n" + q + "@").search(text, i + 2)
            if not m: err("unterminated here-string"); break
            line += text.count("\n", i, m.end()); out.append("\n" * text.count("\n", i, m.end())); i = m.end(); continue
        if c == "`":
            i += 2; continue
        if c == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if text[j + 1:j + 2] == "'": j += 2; continue
                    break
                if text[j] == "\n": line += 1
                j += 1
            else:
                err("unterminated ' string"); break
            out.append(" "); i = j + 1; continue
        if c == '"':
            modes.append("dq"); i += 1; continue
        if c == "$" and text[i + 1:i + 2] == "{":
            j = text.find("}", i); i = j + 1; out.append(" "); continue
        if c in "([{":
            stack.append((c, line))
        elif c in ")]}":
            if not stack or (stack[-1][0] not in (pairs[c], "$(") or (stack[-1][0] == "$(" and c != ")")):
                err("unbalanced '%s' (open stack top: %s)" % (c, stack[-1] if stack else None)); 
            else:
                top = stack.pop()
                if top[0] == "$(" and c == ")" and len(modes) > 1 and modes[-1] == "code":
                    modes.pop()
        out.append(c); i += 1
    for ch, ln in stack: probs.append("line %d: unclosed '%s'" % (ln, ch))
    if len(modes) != 1: probs.append("unterminated string/subexpression (modes %s)" % modes)
    return "".join(out), probs

def main(files):
    defined = set()
    texts = {}
    for f in files:
        t = open(f, "rb").read()
        texts[f] = t
        defined |= set(re.findall(rb"(?mi)^\s*function\s+([\w-]+)", t))
    defined = {d.decode() for d in defined}
    bad = 0
    for f in files:
        raw = texts[f]; problems = []
        if any(b > 127 for b in raw): problems.append("non-ASCII bytes present (offset %d)" % next(i for i, b in enumerate(raw) if b > 127))
        text = raw.decode("ascii", "replace")
        if "\r" in text: problems.append("CR characters present (use LF)")
        code, lp = lex(text)
        problems += lp
        for pat in FORBIDDEN:
            for m in re.finditer(pat, code, re.I):
                problems.append("forbidden/system-changing construct %r at line %d" % (m.group(0), code.count("\n", 0, m.start()) + 1))
        unknown = sorted({m for m in re.findall(r"(?<![\w$.\-:'\"])([A-Z][a-z]+-[A-Z][A-Za-z]+)\b", code)} - KNOWN - defined)
        review = []
        for pat in REVIEW:
            ln = sorted({code.count("\n", 0, m.start()) + 1 for m in re.finditer(pat, code, re.I)})
            if ln: review.append("%s@%s" % (pat, ",".join(map(str, ln[:12])) + ("..." if len(ln) > 12 else "")))
        print("%s: %s" % (f, "OK" if not problems else "PROBLEMS"))
        for p in problems: print("   ! " + p)
        if unknown: print("   ? commands not in the known list nor defined in the kit (typo check): " + ", ".join(unknown))
        print("   review: " + "; ".join(review))
        bad += bool(problems)
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
