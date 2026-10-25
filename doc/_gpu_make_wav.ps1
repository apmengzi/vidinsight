Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.Rate = -2
$s.SetOutputToWaveFile('D:/CyberStomach/VidInsight/doc/_gpu_test.wav')
foreach ($i in 1..12) {
    $s.Speak('Video note generation pipeline downloads the video, extracts audio, transcribes speech and writes structured markdown notes for knowledge workers.')
}
$s.Dispose()
Get-Item 'D:/CyberStomach/VidInsight/doc/_gpu_test.wav' | Select-Object FullName, Length
