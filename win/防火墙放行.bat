@echo off
rem 一键放行易码互传（需管理员权限）
chcp 65001 >nul
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo 需要管理员权限：请右键本文件，选择「以管理员身份运行」。
    pause
    exit /b 1
)
if not exist "%~dp0yima-transfer.exe" (
    echo 未找到 yima-transfer.exe，请把本文件放在 exe 同目录后再运行。
    pause
    exit /b 1
)
netsh advfirewall firewall delete rule name="易码互传" >nul 2>&1
netsh advfirewall firewall delete rule name="易码互传HTTP" >nul 2>&1
netsh advfirewall firewall add rule name="易码互传" dir=in action=allow enable=yes profile=any program="%~dp0yima-transfer.exe" >nul
netsh advfirewall firewall add rule name="易码互传" dir=out action=allow enable=yes profile=any program="%~dp0yima-transfer.exe" >nul
echo 完成：已放行易码互传的入站与出站。现在可以重新打开两端程序测试互传。
pause
