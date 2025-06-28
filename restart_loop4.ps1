# Myria BOTの自動再起動スクリプト
$pythonPath = "C:\Users\81901\AppData\Local\Programs\Python\Python312\python.exe"
$scriptPath = "S:\Python\bot\Sophia\sophia_bot.py"
$waitSeconds = 5

# エンコーディングをUTF-8に設定
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "Sophia BOTの自動再起動を開始します..."

while ($true) {
    Write-Host "BOTを起動します: $scriptPath"
    if (-not (Test-Path $scriptPath)) {
        Write-Host "エラー: スクリプトファイルが見つかりません: $scriptPath"
        break
    }
    & $pythonPath $scriptPath
    Write-Host "BOTプロセスが終了しました。$waitSeconds秒後に再起動します..."
    Start-Sleep -Seconds $waitSeconds
}