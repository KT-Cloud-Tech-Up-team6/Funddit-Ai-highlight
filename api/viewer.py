"""로컬 확인용 뷰어 — 타임라인과 쇼츠를 브라우저에서 본다.

MVP 검증용이다. 실제 FE는 별도로 만든다.
  http://127.0.0.1:8010/view/{job_id}
"""
from __future__ import annotations

VIEWER_HTML = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>방송 다시보기 · 타임라인</title>
<style>
  :root {
    --bg:#0f0e13; --panel:#1a1922; --line:#2c2a36; --fg:#eceaf2;
    --muted:#8f8b9c; --accent:#ff8a5c; --mint:#7fd4a8; --coral:#ff9b9b;
    --lav:#a9b8f0; --cream:#f5e6c8;
  }
  * { box-sizing:border-box; }
  body {
    margin:0; background:var(--bg); color:var(--fg);
    font-family:"Han Santteut Dotum","Malgun Gothic",-apple-system,sans-serif;
    line-height:1.6;
  }
  .wrap { max-width:1180px; margin:0 auto; padding:28px 20px 60px; }
  h1 { font-size:22px; margin:0 0 4px; letter-spacing:-.02em; }
  .sub { color:var(--muted); font-size:13px; margin:0 0 24px; }
  .grid { display:grid; grid-template-columns:1fr 380px; gap:24px; align-items:start; }
  @media (max-width:900px){ .grid{grid-template-columns:1fr;} }

  video { width:100%; border-radius:10px; background:#000; display:block; }

  .bar { position:relative; height:26px; margin-top:10px; border-radius:6px;
         overflow:hidden; background:var(--panel); display:flex; }
  .bar span { display:block; height:100%; cursor:pointer; opacity:.75;
              transition:opacity .12s; border-right:1px solid rgba(0,0,0,.35); }
  .bar span:hover { opacity:1; }
  .legend { display:flex; gap:14px; flex-wrap:wrap; margin-top:10px;
            font-size:11.5px; color:var(--muted); }
  .legend i { display:inline-block; width:10px; height:10px; border-radius:2px;
              margin-right:5px; vertical-align:-1px; }

  .panel { background:var(--panel); border:1px solid var(--line);
           border-radius:10px; overflow:hidden; }
  .panel h2 { font-size:13px; margin:0; padding:12px 16px; color:var(--muted);
              border-bottom:1px solid var(--line); font-weight:600; }
  .list { max-height:560px; overflow-y:auto; }
  .item { display:flex; gap:12px; padding:11px 16px; cursor:pointer;
          border-bottom:1px solid var(--line); transition:background .12s; }
  .item:hover { background:#22202c; }
  .item.on { background:#2a2736; }
  .item:last-child { border-bottom:none; }
  .ts { font-variant-numeric:tabular-nums; font-size:12.5px; color:var(--accent);
        min-width:46px; font-weight:700; }
  .body { flex:1; min-width:0; }
  .ttl { font-size:13.5px; margin-bottom:2px; }
  .meta { font-size:11px; color:var(--muted); }
  .tag { display:inline-block; font-size:10.5px; padding:1px 7px; border-radius:9px;
         margin-right:6px; background:#332f40; color:var(--fg); }

  .shorts { display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr));
            gap:14px; margin-top:14px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px;
          overflow:hidden; }
  .card video { border-radius:0; }
  .card .info { padding:10px 12px; }
  .card .t { font-size:12.5px; margin-bottom:3px; }
  .card .m { font-size:11px; color:var(--muted); }
  h3 { font-size:15px; margin:34px 0 0; }
</style>
</head>
<body>
<div class="wrap">
  <h1 id="jobTitle">방송 다시보기</h1>
  <p class="sub" id="jobSub">불러오는 중…</p>

  <div class="grid">
    <div>
      <video id="v" controls preload="metadata"></video>
      <div class="bar" id="bar"></div>
      <div class="legend" id="legend"></div>

      <h3>하이라이트 쇼츠</h3>
      <div class="shorts" id="shorts"></div>
    </div>

    <div class="panel">
      <h2>타임라인 · <span id="chCount">0</span>개 챕터</h2>
      <div class="list" id="chapters"></div>
    </div>
  </div>
</div>

<script>
const JOB = location.pathname.split('/').pop();
const COLORS = {
  intro:'#8f8b9c', feature:'#7fd4a8', demo:'#a9b8f0', spec:'#7fd4a8',
  funding:'#ff9b9b', qna:'#ffc07f', story:'#c9a9f0', closing:'#6f6b7c'
};
const v = document.getElementById('v');
let chapters = [];

function fmt(ms){
  const s = Math.floor(ms/1000), m = Math.floor(s/60);
  return `${String(m).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`;
}

async function load(){
  v.src = `/files/${JOB}/input/video.mp4`;

  const tl = await fetch(`/jobs/${JOB}/timeline`).then(r=>r.json());
  chapters = tl.chapters;
  document.getElementById('chCount').textContent = chapters.length;

  const total = chapters[chapters.length-1].end_ms;
  document.getElementById('jobSub').textContent =
    `${fmt(total)} · 챕터 ${chapters.length}개 · 클릭하면 해당 지점으로 이동합니다`;

  // 타임라인 바
  const bar = document.getElementById('bar');
  bar.innerHTML = '';
  chapters.forEach((c,i)=>{
    const el = document.createElement('span');
    el.style.width = ((c.end_ms-c.start_ms)/total*100)+'%';
    el.style.background = COLORS[c.category] || '#6f6b7c';
    el.title = `${c.timestamp} ${c.title}`;
    el.onclick = ()=>seek(i);
    bar.appendChild(el);
  });

  // 범례
  const used = [...new Set(chapters.map(c=>c.category))];
  document.getElementById('legend').innerHTML = used.map(cat=>{
    const name = chapters.find(c=>c.category===cat).category_name;
    return `<span><i style="background:${COLORS[cat]||'#6f6b7c'}"></i>${name}</span>`;
  }).join('');

  // 챕터 목록
  const list = document.getElementById('chapters');
  list.innerHTML = chapters.map((c,i)=>`
    <div class="item" data-i="${i}" onclick="seek(${i})">
      <div class="ts">${c.timestamp}</div>
      <div class="body">
        <div class="ttl">${c.title}</div>
        <div class="meta"><span class="tag" style="color:${COLORS[c.category]||'#fff'}">${c.category_name}</span>${Math.round(c.duration_sec)}초</div>
      </div>
    </div>`).join('');

  // 쇼츠
  try {
    const sh = await fetch(`/jobs/${JOB}/shorts`).then(r=>r.json());
    document.getElementById('shorts').innerHTML = sh.shorts.map(s=>`
      <div class="card">
        <video src="${s.video_url}" controls preload="metadata"></video>
        <div class="info">
          <div class="t">${s.title}</div>
          <div class="m">${s.part_type} · ${Math.round(s.duration_sec)}초 · 자막 ${s.captions.length}개</div>
        </div>
      </div>`).join('');
  } catch(e){
    document.getElementById('shorts').innerHTML =
      '<p style="color:#8f8b9c;font-size:13px">아직 생성된 쇼츠가 없습니다.</p>';
  }
}

function seek(i){
  v.currentTime = chapters[i].start_ms/1000;
  v.play();
  document.querySelectorAll('.item').forEach(el=>el.classList.remove('on'));
  document.querySelector(`.item[data-i="${i}"]`)?.classList.add('on');
}

// 재생 위치에 맞춰 현재 챕터 표시
v.addEventListener('timeupdate', ()=>{
  const ms = v.currentTime*1000;
  const i = chapters.findIndex(c=>ms>=c.start_ms && ms<c.end_ms);
  if(i<0) return;
  document.querySelectorAll('.item').forEach(el=>el.classList.remove('on'));
  document.querySelector(`.item[data-i="${i}"]`)?.classList.add('on');
});

load();
</script>
</body>
</html>
"""
