# 하이라이트 쇼츠·타임라인 AI 서버
#
# GPU 가 필요하다. Whisper large-v3 가 20분 영상에 GPU 5분, CPU 는 수 배 걸린다.
# CUDA 12 런타임 이미지를 쓰고, cuDNN 은 pip 휠(nvidia-cudnn-cu12)로 받는다.
#
# 빌드:  docker build -t funddit-ai-highlight .
# 실행:  docker run --gpus all -p 8000:8000 --env-file .env funddit-ai-highlight
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1

# ffmpeg: 렌더링·프레임 추출에 필수
# fonts-nanum: 자막 폰트. 없으면 libass 가 한글을 네모로 그린다
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3-pip ffmpeg fonts-nanum ca-certificates \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성을 먼저 복사해 레이어 캐시를 살린다 (Whisper·CUDA 휠이 무겁다)
COPY requirements.txt .
RUN python3.11 -m pip install --no-cache-dir -r requirements.txt

COPY api/ ./api/
COPY poc/ ./poc/

# 작업 산출물 위치. k8s 에서는 PVC 를 여기에 마운트한다.
ENV SHORTS_WORKSPACE=/data/workspace \
    LOG_FORMAT=json \
    LOG_LEVEL=INFO
RUN mkdir -p /data/workspace

EXPOSE 8000

# 컨테이너 헬스체크. k8s 는 별도로 livenessProbe 를 건다.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python3.11 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/v1/ai/health',timeout=4)" || exit 1

# 워커 1개인 이유: Whisper 모델을 프로세스마다 로드하면 GPU 메모리가 배로 든다.
# 동시 처리는 SHORTS_MAX_JOBS(기본 2)가 프로세스 안에서 제한한다.
CMD ["python3.11", "-m", "uvicorn", "api.main:app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
