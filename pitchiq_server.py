import json, math, os, urllib.request, urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

FPL="https://fantasy.premierleague.com/api"
POS={1:"GK",2:"DEF",3:"MID",4:"FWD"}

def fetch(path):
    req=urllib.request.Request(FPL+path,headers={"User-Agent":"FPL-PitchIQ/1.0","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=15) as r:
        return json.loads(r.read())

def num(v,d=0.0):
    try:
        x=float(v); return x if math.isfinite(x) else d
    except: return d

def event_state(events):
    playable=[e for e in events if not e.get("finished")]
    return next((e for e in playable if e.get("is_current")), next((e for e in playable if e.get("is_next")), playable[0] if playable else None))

def fixture_factor(fixtures, team_id, event_id, horizon):
    fs=[f for f in fixtures if f.get("event") and event_id<=int(f["event"])<event_id+horizon and (f.get("team_h")==team_id or f.get("team_a")==team_id)]
    if not fs: return 1.0,3.0
    ds=[num(f.get("team_h_difficulty") if f.get("team_h")==team_id else f.get("team_a_difficulty"),3) for f in fs]
    avg=sum(ds)/len(ds)
    return max(.72,min(1.30,1+(3.0-avg)*.11)),avg

def player_score(p,fixtures,event_id,horizon):
    ep=num(p.get("ep_next"))
    form=num(p.get("form"))
    chance=p.get("chance_of_playing_next_round")
    availability=1.0 if chance is None else max(0,min(1,num(chance)/100))
    fixture,_=fixture_factor(fixtures,int(p["team"]),event_id,horizon)
    form_adj=max(.75,min(1.25,1+(form-3.0)*.045))
    base=max(0.0,ep)*availability*fixture*form_adj
    return base

def build_team(team_id):
    b=fetch("/bootstrap-static/")
    ev=event_state(b.get("events",[]))
    if not ev: raise ValueError("No playable gameweek is available.")
    gw=int(ev["id"])
    entry=fetch(f"/entry/{team_id}/")
    picks=fetch(f"/entry/{team_id}/event/{gw}/picks/")
    fixtures=fetch("/fixtures/")
    players={int(p["id"]):p for p in b.get("elements",[])}
    teams={int(t["id"]):t for t in b.get("teams",[])}
    squad=[]
    for pick in picks.get("picks",[]):
        p=players.get(int(pick["element"]))
        if not p: continue
        squad.append({
            "id":p["id"],"name":p.get("web_name") or (p.get("first_name","")+" "+p.get("second_name","")),
            "position":POS.get(int(p["element_type"]),"UNK"),"positionId":int(p["element_type"]),
            "teamId":int(p["team"]),"team":teams.get(int(p["team"]),{}).get("short_name","?"),
            "status":p.get("status",""),"price":num(pick.get("selling_price"),num(p.get("now_cost")))/10,
            "marketPrice":num(p.get("now_cost"))/10,"chance":p.get("chance_of_playing_next_round"),
            "epNext":num(p.get("ep_next")),"form":num(p.get("form")),"minutes":int(num(p.get("minutes"))),
            "score1":round(player_score(p,fixtures,gw,1),2),
            "score3":round(player_score(p,fixtures,gw,3)*3,2),
            "score6":round(player_score(p,fixtures,gw,6)*6,2),
        })
    history=picks.get("entry_history") or {}
    bank=num(history.get("bank"),num(entry.get("last_deadline_bank")))/10
    transfers=int(num(history.get("event_transfers")))
    # Public data exposes the latest deadline state and this event's transfer count,
    # but not a guaranteed pre-deadline free-transfer balance.
    free_unknown=True
    clubs={}
    for p in squad: clubs[p["teamId"]]=clubs.get(p["teamId"],0)+1

    rec=[]
    pool=list(players.values())
    for sold in squad:
        budget=bank+sold["price"]
        for buy in pool:
            if int(buy["id"])==sold["id"] or int(buy["element_type"])!=sold["positionId"]: continue
            if buy.get("status") not in ("a","d"): continue
            price=num(buy.get("now_cost"))/10
            if price>budget+1e-9: continue
            if clubs.get(int(buy["team"]),0)-(1 if int(buy["team"])==sold["teamId"] else 0)>=3: continue
            buy1=player_score(buy,fixtures,gw,1)
            buy3=player_score(buy,fixtures,gw,3)*3
            buy6=player_score(buy,fixtures,gw,6)*6
            sell1=sold["score1"]; sell3=sold["score3"]; sell6=sold["score6"]
            raw=buy1-sell1
            availability=(buy.get("chance_of_playing_next_round") if buy.get("chance_of_playing_next_round") is not None else 100)
            risk=(100-num(availability))/100
            fixture_buy,fd_buy=fixture_factor(fixtures,int(buy["team"]),gw,3)
            fixture_sell,fd_sell=fixture_factor(fixtures,sold["teamId"],gw,3)
            score=raw + (buy3-sell3)*.12 + (buy6-sell6)*.05 + (fixture_buy-fixture_sell)*.35 - risk*.45
            if score<=0.15: continue
            confidence=max(.25,min(.90,.48+min(.28,abs(score)*.10)-risk*.25))
            reasons=[]
            if buy1>sell1+.15: reasons.append("higher projected next-GW output")
            if fixture_buy>fixture_sell+.03: reasons.append("better fixture run")
            if num(buy.get("form"))>num(sold.get("form"))+.5: reasons.append("stronger recent form")
            if (buy.get("chance_of_playing_next_round") or 100)>(sold.get("chance_of_playing_next_round") or 100)+10: reasons.append("lower availability risk")
            if not reasons: reasons=["higher risk-adjusted projection"]
            rec.append({
                "sell":sold["name"],"buy":buy.get("web_name",""),"position":sold["position"],
                "netExpectedGain":round(score,2),"gain1":round(buy1-sell1,2),
                "gain3":round(buy3-sell3,2),"gain6":round(buy6-sell6,2),
                "confidence":round(confidence,2),"hitCost":None,
                "bankAfter":round(budget-price,1),"expectedMinutesDifference":None,
                "fixtureImpact":round((fixture_buy-fixture_sell)*.35,2),
                "reason":" + ".join(reasons[:3]),"risk":"availability risk" if risk>.2 else ("fixture uncertainty" if fd_buy>4 else "model uncertainty"),
                "horizon":"1/3/6 GW","dataTimestamp":datetime.now(timezone.utc).isoformat()
            })
    rec.sort(key=lambda x:x["netExpectedGain"],reverse=True)
    return {
      "teamId":team_id,"event":gw,"eventName":ev.get("name"),"deadline":ev.get("deadline_time"),
      "retrievedAt":datetime.now(timezone.utc).isoformat(),"bank":round(bank,1),
      "eventTransfers":transfers,"freeTransfers":"unverified",
      "players":squad,"opportunities":rec[:5],
      "baseline":{"action":"DO NOTHING","reason":"Roll is the reference decision. A transfer is surfaced only when the estimated risk-adjusted improvement clears the decision threshold."},
      "model":{"version":"live-heuristic-1.0","calibration":"collecting",
        "method":"Official FPL expected-points signal + form + fixture difficulty + availability adjustment.",
        "limitation":"This production layer is not yet statistically calibrated. Historical accuracy is intentionally not claimed."}
    }

HTML='''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>FPL PitchIQ — Your best FPL move, explained.</title><style>
:root{color-scheme:dark;--bg:#07110d;--card:#0d1b15;--line:#20372c;--text:#eef7f1;--muted:#91a89c;--accent:#77e39c;--danger:#ff9b9b}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 20% 0,#123322,#07110d 45%);font-family:Inter,system-ui,sans-serif;color:var(--text)}main{max-width:1180px;margin:auto;padding:24px}.top{display:flex;justify-content:space-between;align-items:center;padding:8px 0 48px}.brand{font-weight:800;font-size:20px}.badge{font-size:11px;color:var(--accent);border:1px solid var(--line);padding:8px 12px;border-radius:999px}.hero{max-width:850px;padding:30px 0 40px}.ey{font-size:11px;letter-spacing:.16em;color:var(--accent);font-weight:800}.hero h1{font-size:clamp(44px,7vw,82px);line-height:.98;margin:14px 0}.hero h1 em{color:var(--accent);font-style:normal}.hero p{font-size:18px;color:var(--muted);max-width:700px;line-height:1.6}.import{margin-top:28px;max-width:620px}.import label{display:block;font-size:12px;font-weight:700;margin-bottom:8px}.row{display:flex;gap:10px}input{flex:1;background:#08150f;border:1px solid var(--line);border-radius:10px;color:white;padding:15px;font-size:16px}button{background:var(--accent);border:0;border-radius:10px;padding:0 20px;font-weight:800;cursor:pointer}button:disabled{opacity:.5}.small,.muted{color:var(--muted);font-size:12px}.error{color:var(--danger);margin-top:12px}.trust{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);border:1px solid var(--line);border-radius:14px;overflow:hidden}.trust div{background:#0a1711;padding:18px}.trust b{display:block}.trust span{display:block;color:var(--muted);font-size:12px;margin-top:5px}.section{padding:54px 0}.head{display:flex;justify-content:space-between;align-items:end;margin-bottom:18px}.head h2{margin:6px 0;font-size:32px}.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}.card{background:rgba(13,27,21,.92);border:1px solid var(--line);border-radius:16px;padding:20px}.baseline{margin-bottom:14px}.label{font-size:10px;letter-spacing:.14em;color:var(--accent);font-weight:800}.metrics{display:flex;gap:28px;margin:18px 0}.metrics b{display:block;font-size:22px}.metrics span{color:var(--muted);font-size:11px}.player{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid #193026}.player:last-child{border-bottom:0}.player small{display:block;color:var(--muted);margin-top:3px}.pill{border:1px solid var(--line);padding:7px 10px;border-radius:999px;color:var(--muted);font-size:12px}.footer{padding:50px 0;color:var(--muted);font-size:12px}@media(max-width:760px){.trust,.grid{grid-template-columns:1fr}.row{flex-direction:column}button{height:50px}.hero h1{font-size:48px}.metrics{gap:15px}}
</style></head><body><main><header class="top"><div class="brand">IQ · FPL PitchIQ</div><div class="badge">LIVE OFFICIAL FPL DATA</div></header><section class="hero"><div class="ey">DECISION ENGINE · EXPLAINABLE FPL</div><h1>Your best FPL move,<br><em>explained.</em></h1><p>Import your public FPL Team ID and compare legal transfer opportunities against Roll — with projected gain, fixtures, availability, cost and uncertainty visible.</p><div class="import"><label>PUBLIC FPL TEAM ID</label><div class="row"><input id="id" placeholder="e.g. 1234567" inputmode="numeric"><button id="go">Analyze my team</button></div><p class="small">No FPL password or cookie is required.</p><div id="err"></div></div></section><section class="trust"><div><b id="gw">Dynamic GW</b><span>current playable event</span></div><div><b id="sync">Awaiting squad</b><span>official FPL retrieval</span></div><div><b>Roll first</b><span>do-nothing baseline</span></div><div><b>No fake accuracy</b><span>calibration status visible</span></div></section><section class="section" id="out" style="display:none"><div class="head"><div><div class="ey">YOUR DECISION</div><h2>Best Transfer Opportunities</h2></div><div class="pill" id="event"></div></div><div class="card baseline"><div class="label">BASELINE</div><h3>DO NOTHING / ROLL</h3><p id="base"></p></div><div class="grid" id="recs"></div><div class="card" style="margin-top:14px"><div class="label">YOUR SQUAD</div><div id="squad"></div></div><p class="muted" id="meta"></p></section><section class="section"><div class="ey">MODEL PERFORMANCE</div><h2>Evidence before accuracy claims.</h2><p class="muted">Historical accuracy is not shown until pre-deadline walk-forward evaluation has enough completed forecasts. The current live layer is explicitly marked heuristic.</p></section><footer class="footer">FPL PitchIQ · Public Team ID only · No FPL password required</footer></main><script>
const $=id=>document.getElementById(id);function esc(s){return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]))}
$("go").onclick=async()=>{const id=Number($("id").value);$("err").textContent="";if(!Number.isInteger(id)||id<1){$("err").textContent="Enter a valid public FPL Team ID.";return}$("go").disabled=true;$("go").textContent="Analyzing…";try{const r=await fetch("/api/team/"+id);const d=await r.json();if(!r.ok)throw Error(d.error||"Unable to load team");$("gw").textContent="GW "+d.event;$("sync").textContent="Synced";$("event").textContent="GW "+d.event;$("base").textContent=d.baseline.reason;$("recs").innerHTML=d.opportunities.length?d.opportunities.map((o,i)=>`<article class="card"><div class="label">#${i+1} · ${esc(o.position)}</div><h3>SELL ${esc(o.sell)} → BUY ${esc(o.buy)}</h3><div class="metrics"><div><b>+${o.netExpectedGain}</b><span>NET EXPECTED</span></div><div><b>${Math.round(o.confidence*100)}%</b><span>CONFIDENCE</span></div><div><b>—</b><span>HIT COST</span></div></div><p><b>${esc(o.reason)}.</b> ${esc(o.risk)}.</p><div class="small">1 GW +${o.gain1} · 3 GW +${o.gain3} · 6 GW +${o.gain6} · Bank after £${o.bankAfter.toFixed(1)}m</div></article>`).join(""):'<div class="card"><h3>DO NOTHING</h3><p>No transfer currently clears the decision threshold.</p></div>';$("squad").innerHTML=d.players.map(p=>`<div class="player"><span><b>${esc(p.name)}</b><small>${esc(p.position)} · ${esc(p.team)} · ${esc(p.status)}</small></span><span>£${p.price.toFixed(1)}m</span></div>`).join("");$("meta").textContent=`Retrieved ${new Date(d.retrievedAt).toLocaleString()} · Model ${d.model.version} · ${d.model.calibration}. Free transfers remain unverified from public data.`;$("out").style.display="block";$("out").scrollIntoView({behavior:"smooth"})}catch(e){$("err").textContent=e.message}finally{$("go").disabled=false;$("go").textContent="Analyze my team"}};</script></body></html>'''

class Handler(BaseHTTPRequestHandler):
    def send_json(self,data,status=200):
        body=json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status);self.send_header("Content-Type","application/json");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
    def do_GET(self):
        p=urlparse(self.path).path
        try:
            if p=="/" or p=="/index.html":
                b=HTML.encode();self.send_response(200);self.send_header("Content-Type","text/html; charset=utf-8");self.send_header("Content-Length",str(len(b)));self.end_headers();self.wfile.write(b);return
            if p=="/api/health": self.send_json({"status":"ok","service":"fpl-pitchiq","version":"live-heuristic-1.0","timestamp":datetime.now(timezone.utc).isoformat()});return
            if p.startswith("/api/team/"):
                tid=int(p.rsplit("/",1)[1])
                if tid<=0: raise ValueError("Invalid Team ID")
                self.send_json(build_team(tid));return
            if p=="/api/model-performance":
                self.send_json({"status":"collecting","model_version":"live-heuristic-1.0","evaluated_gameweeks":0,"sample_size":0,"claims":[],"note":"No historical accuracy claim is published until walk-forward pre-deadline evaluation is connected."});return
            self.send_json({"error":"Not found"},404)
        except urllib.error.HTTPError as e:
            self.send_json({"error":f"Official FPL request failed ({e.code})"},502)
        except Exception as e:
            self.send_json({"error":str(e)},400)
    def log_message(self,fmt,*args): print("[pitchiq]",fmt%args)

if __name__=="__main__":
    port=int(os.environ.get("PORT","8080"))
    ThreadingHTTPServer(("0.0.0.0",port),Handler).serve_forever()
