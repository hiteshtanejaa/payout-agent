param(
    [Parameter(Mandatory = $true)]
    [string]$TextPath,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath,
    [string]$Voice = "Microsoft Zira Desktop",
    [ValidateRange(-10, 10)]
    [int]$Rate = 2,
    [ValidateRange(0, 100)]
    [int]$Volume = 100
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech

$inputFile = (Resolve-Path -LiteralPath $TextPath).Path
$outputFile = [System.IO.Path]::GetFullPath($OutputPath)
$outputDirectory = [System.IO.Path]::GetDirectoryName($outputFile)
[System.IO.Directory]::CreateDirectory($outputDirectory) | Out-Null
$narrationText = [System.IO.File]::ReadAllText($inputFile, [System.Text.Encoding]::UTF8)

$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $speaker.SelectVoice($Voice)
    $speaker.Rate = $Rate
    $speaker.Volume = $Volume
    $speaker.SetOutputToWaveFile($outputFile)
    $speaker.Speak($narrationText)
} finally {
    $speaker.Dispose()
}

$waveStream = [System.IO.File]::OpenRead($outputFile)
$reader = New-Object System.IO.BinaryReader($waveStream)
try {
    $reader.BaseStream.Position = 12
    $byteRate = 0
    $dataSize = 0
    while ($reader.BaseStream.Position + 8 -le $reader.BaseStream.Length) {
        $chunkName = [System.Text.Encoding]::ASCII.GetString($reader.ReadBytes(4))
        $chunkSize = $reader.ReadUInt32()
        $nextPosition = $reader.BaseStream.Position + $chunkSize + ($chunkSize % 2)
        if ($chunkName -eq 'fmt ') {
            $reader.BaseStream.Position += 8
            $byteRate = $reader.ReadUInt32()
        } elseif ($chunkName -eq 'data') {
            $dataSize += $chunkSize
        }
        $reader.BaseStream.Position = $nextPosition
    }
} finally {
    $reader.Dispose()
    $waveStream.Dispose()
}

[PSCustomObject]@{
    output = $outputFile
    voice = $Voice
    rate = $Rate
    duration_seconds = if ($byteRate -gt 0) { [Math]::Round($dataSize / $byteRate, 3) } else { $null }
    word_count = ([regex]::Matches($narrationText, '\S+')).Count
} | ConvertTo-Json
