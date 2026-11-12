"""Shared look of the panel and the setup pages."""

# Art direction: a projection booth at night. Deep blue ink, a serif for names and figures, and two lamp colours
# that always mean the same thing: amber = your side (torrent), teal = the link to B. Ivory marks "you are here".
CSS = """
:root{--bg:#111725;--bar:#151c2c;--surface:#19223a;--raised:#212b45;--line:#29344f;--soft:#1f2840;
--text:#eef1f7;--muted:#8d98b0;--faint:#66728c;--ivory:#f2e6cc;
--dl:#e8a23a;--tx:#38b6c9;--ok:#5cc48a;--bad:#e2656f;
--serif:"Iowan Old Style","Charter","Palatino Linotype","Book Antiqua",Georgia,serif;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14.5px/1.5 var(--sans);font-variant-numeric:tabular-nums}
a{color:inherit}
:focus-visible{outline:2px solid var(--tx);outline-offset:2px;border-radius:4px}
.top{position:sticky;top:0;z-index:5;background:var(--bar);border-bottom:1px solid var(--line)}
.top .in,.page{max-width:1240px;margin:0 auto;padding:0 28px}
.top .in{display:flex;align-items:stretch;gap:44px;height:62px}
.brand{display:flex;align-items:center;gap:11px;font:600 21px/1 var(--serif);color:var(--ivory);text-decoration:none}
.tabs{display:flex;gap:2px}
.tabs a{display:flex;align-items:center;padding:0 16px;color:var(--muted);text-decoration:none;font-weight:500;
border-bottom:2px solid transparent;margin-bottom:-1px}
.tabs a:hover{color:var(--text)}
.tabs a[aria-current=page]{color:var(--text);border-bottom-color:var(--ivory)}
.live{margin-left:auto;display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--muted)}
.live i{width:7px;height:7px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 3px rgba(92,196,138,.18)}
body.stale .live i{background:var(--bad);box-shadow:0 0 0 3px rgba(226,101,111,.18)}
body.stale .live span::after{content:", connexion perdue"}
.page{padding-top:36px;padding-bottom:72px}
.head{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;flex-wrap:wrap;margin-bottom:22px}
h2{margin:0;font:600 34px/1.1 var(--serif);letter-spacing:-.01em}
.sub{margin:6px 0 0;color:var(--muted);max-width:66ch}
h3{margin:0 0 14px;font:600 19px/1.2 var(--serif)}
.warn{background:rgba(232,162,58,.1);border:1px solid rgba(232,162,58,.45);color:#f6d9a8;padding:10px 14px;
border-radius:10px;margin:0 0 18px}
.seg{display:inline-flex;gap:2px;background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:3px;
flex-wrap:wrap}
.seg a{padding:6px 13px;border-radius:7px;color:var(--muted);text-decoration:none;font-size:13.5px}
.seg a b{margin-left:7px;color:var(--faint);font-weight:600}
.seg a.on{background:var(--raised);color:var(--text)}
.seg a.on b{color:var(--ivory)}
.card{background:var(--surface);border:1px solid var(--line);border-radius:14px}
.cols-h,li.job{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(0,2fr) minmax(0,1fr) 196px;gap:28px;
align-items:center;padding:0 24px}
.cols-h{height:42px;font-size:12.5px;color:var(--faint);border-bottom:1px solid var(--line)}
ul.jobs{list-style:none;margin:0;padding:0}
li.job{padding-top:18px;padding-bottom:18px;border-top:1px solid var(--soft)}
li.job:first-child{border-top:0}
li.dim{opacity:.55}
.t{font-weight:650;font-size:15px;overflow-wrap:anywhere}
.meta{color:var(--muted);font-size:12.5px;margin-top:2px}
.err{color:var(--bad);font-size:12.5px;margin-top:4px}
.line{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:8px}
.tag{font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:5px;padding:0 6px}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;font-weight:600;padding:1px 9px 1px 7px;
border-radius:999px;background:var(--raised);color:var(--text)}
.pill::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--c,var(--muted))}
.p-dl{--c:var(--dl)}.p-tx{--c:var(--tx)}.p-ok{--c:var(--ok)}.p-bad{--c:var(--bad)}.p-lib{--c:var(--ivory)}
.bar+.bar{margin-top:9px}
.bar .lbl{display:flex;justify-content:space-between;gap:10px;font-size:12px;color:var(--muted)}
.bar .lbl b{color:var(--text);font-weight:600;text-align:right}
.track{height:7px;border-radius:4px;background:#121a2b;overflow:hidden;margin-top:4px}
.fill{height:100%;border-radius:4px;transition:width .8s ease}
.dl .fill{background:var(--dl)}.tx .fill{background:var(--tx)}.seed .fill{background:var(--ok)}
.done .fill{opacity:.4}
.run .fill{background-image:linear-gradient(90deg,transparent,rgba(255,255,255,.3),transparent);
background-size:200% 100%;animation:run 1.6s linear infinite}
@keyframes run{from{background-position:200% 0}to{background-position:-200% 0}}
.rm{font-size:12.5px;margin-top:8px;color:var(--muted)}
.rm b{color:var(--text);font-weight:600}
.acts{display:flex;gap:6px;justify-content:flex-end;flex-wrap:wrap}
form{margin:0}
button{font:500 13px var(--sans);color:var(--text);background:var(--raised);border:1px solid var(--line);
border-radius:8px;padding:6px 13px;cursor:pointer}
button:hover{border-color:var(--muted)}
button.del{color:#f2a0a6}
.empty{color:var(--muted);padding:56px 24px;text-align:center;margin:0}
.figs{display:grid;grid-template-columns:repeat(4,minmax(0,1fr))}
.fig{padding:22px 24px;border-left:1px solid var(--line)}
.fig:first-child{border-left:0}
.fig span{display:block;color:var(--muted);font-size:13px}
.fig b{display:block;font:600 32px/1.15 var(--serif);margin:6px 0 4px}
.fig small{color:var(--faint);font-size:12.5px}
.block{padding:22px 24px;margin-top:20px}
.chart{display:flex;align-items:flex-end;gap:2px;height:170px;border-bottom:1px solid var(--line);position:relative;
margin-top:18px}
.chart .grid{position:absolute;left:0;right:0;top:0;border-top:1px dashed var(--line);font-size:11.5px;
color:var(--faint);padding-top:2px}
.col{flex:1;height:100%;display:flex;align-items:flex-end;position:relative}
.col i{display:block;width:100%;background:var(--tx);border-radius:4px 4px 0 0}
.col:hover i,.col:focus i{background:#6fd2df}
.col .tip{display:none;position:absolute;bottom:calc(100% + 6px);left:50%;transform:translateX(-50%);white-space:nowrap;
background:var(--raised);border:1px solid var(--line);border-radius:8px;padding:5px 9px;font-size:12px;z-index:2}
.col:hover .tip,.col:focus .tip{display:block}
.axis{display:flex;justify-content:space-between;font-size:11.5px;color:var(--faint);margin-top:6px}
.three{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px}
.three .card{padding:22px 24px;margin-top:20px}
dl{margin:0}
dl div{display:flex;justify-content:space-between;gap:12px;padding:8px 0;border-top:1px solid var(--soft)}
dl div:first-child{border-top:0}
dt{color:var(--muted)}dd{margin:0;font-weight:600;text-align:right}
table{width:100%;border-collapse:collapse}
th{font-weight:500;font-size:12.5px;color:var(--faint);text-align:left;padding:0 12px 10px}
td{padding:11px 12px;border-top:1px solid var(--soft)}
th:first-child,td:first-child{padding-left:0}
td.r,th.r{text-align:right}
@media (max-width:960px){.figs{grid-template-columns:repeat(2,1fr)}.fig:nth-child(3){border-left:0}
.fig:nth-child(n+3){border-top:1px solid var(--line)}.three{grid-template-columns:1fr}}
@media (max-width:820px){.cols-h{display:none}li.job{grid-template-columns:1fr;gap:12px}.acts{justify-content:flex-start}
.top .in{gap:16px;padding:0 16px}.page{padding:24px 16px 48px}.live span{display:none}.tabs a{padding:0 10px}
table{font-size:13px}}
@media (prefers-reduced-motion:reduce){.fill{transition:none}.run .fill{animation:none}}
"""

LOGO = ('<svg width="34" height="16" viewBox="0 0 34 16" aria-hidden="true"><line x1="8" y1="8" x2="26" y2="8" '
        'stroke="#66728c" stroke-width="2" stroke-dasharray="2 3"/><circle cx="7" cy="8" r="5" fill="#e8a23a"/>'
        '<circle cx="27" cy="8" r="5" fill="#38b6c9"/></svg>')
