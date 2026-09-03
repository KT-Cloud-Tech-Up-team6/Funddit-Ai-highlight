# 실제 방송(로보락 F25)으로 CFG-1/2/3 실행. 사용 전 .env 값을 환경변수로 로드할 것:
#   Get-Content .env | ? { $_ -match '^\s*[^#].*=' } | % { $k,$v = $_ -split '=',2; [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim()) }
#
#   .\run_real.ps1 stt          # Whisper small/large-v3 + (자격증명 있으면) Google STT → out/real/transcript_*.json
#   .\run_real.ps1 m1 large     # STT 결과 하나 골라 M1 (Flash-Lite, Flash 두 번)
#   .\run_real.ps1 m2 large P1  # 선택 파트로 M2 + 렌더링 (두 모델)
param([string]$step = "stt", [string]$stt = "large", [string]$pick = "P1")

$video = "data/input/roborock_f25.mp4"
$terms = "data/real/product_terms.json"
$out = "out/real"
New-Item -ItemType Directory -Force $out | Out-Null

switch ($step) {
  "stt" {
    python -m poc.pipeline stt --video $video --out "$out/transcript_small.json" --model small --terms $terms
    python -m poc.pipeline stt --video $video --out "$out/transcript_large.json" --model large-v3 --terms $terms
    if ($env:GOOGLE_CLOUD_PROJECT) {
      python -m poc.pipeline stt --engine google --video $video --out "$out/transcript_google.json" --terms $terms
    } else { Write-Host "GOOGLE_CLOUD_PROJECT 없음 → Google STT 생략 (CFG-2/3 불가)" }
    python -m eval.compare_stt (Get-ChildItem "$out/transcript_*.json" | % FullName)
  }
  "m1" {
    foreach ($m in @("gemini-3.5-flash-lite", "gemini-3.7-flash")) {
      $env:GEMINI_MODEL = $m
      $tag = ($m -replace 'gemini-','')
      python -m poc.pipeline m1 --transcript "$out/transcript_$stt.json" --out "$out/segments_${stt}_$tag.json"
    }
  }
  "m2" {
    foreach ($m in @("gemini-3.5-flash-lite", "gemini-3.7-flash")) {
      $env:GEMINI_MODEL = $m
      $tag = ($m -replace 'gemini-','')
      $dir = "$out/${stt}_$tag"
      python -m poc.pipeline m2 --segments "$out/segments_${stt}_$tag.json" --pick $pick --transcript "$out/transcript_$stt.json" --terms $terms --outdir $dir
      python -m poc.pipeline render --video $video --captions "$dir/$($pick.ToLower())_captions.json" --out "$dir/short_$($pick.ToLower()).mp4" --crop-cx 0.62
    }
  }
}
