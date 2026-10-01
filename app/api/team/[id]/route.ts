import { NextResponse } from "next/server";

const FPL = "https://fantasy.premierleague.com/api";

type Player = {
  id:number; web_name:string; first_name:string; second_name:string; element_type:number;
  now_cost:number; ep_next?:string|number; form?:string|number; minutes?:number;
  chance_of_playing_next_round?:number|null; status:string; team:number;
};

function n(v: unknown, fallback=0){ const x=Number(v); return Number.isFinite(x)?x:fallback; }

async function fpl(path:string){
  const r = await fetch(FPL + path, { cache:"no-store", headers:{accept:"application/json"} });
  if(!r.ok) throw new Error("FPL data request failed ("+r.status+")");
  return r.json();
}

export async function GET(_:Request, ctx:{params:Promise<{id:string}>}){
  const {id} = await ctx.params;
  const teamId = Number(id);
  if(!Number.isInteger(teamId) || teamId<=0) return NextResponse.json({error:"Invalid public FPL Team ID."},{status:400});

  try {
    const bootstrap = await fpl("/bootstrap-static/");
    const events = bootstrap.events ?? [];
    const event = events.find((e:any)=>e.is_current) ?? events.find((e:any)=>e.is_next) ?? events.find((e:any)=>!e.finished);
    if(!event) throw new Error("No playable FPL gameweek is available.");

    const [entry,picksData,fixtures] = await Promise.all([
      fpl("/entry/"+teamId+"/"),
      fpl("/entry/"+teamId+"/event/"+event.id+"/picks/"),
      fpl("/fixtures/")
    ]);

    const players:Player[] = bootstrap.elements ?? [];
    const teams = new Map((bootstrap.teams??[]).map((t:any)=>[t.id,t.name]));
    const byId = new Map(players.map(p=>[p.id,p]));
    const squad = (picksData.picks??[]).map((pick:any)=>{
      const p=byId.get(pick.element); if(!p) return null;
      return {
        id:p.id, name:p.web_name || (p.first_name+" "+p.second_name), position:["","GK","DEF","MID","FWD"][p.element_type],
        price:n(pick.selling_price)/10 || n(p.now_cost)/10, marketPrice:n(p.now_cost)/10,
        team:teams.get(p.team) ?? "Unknown", status:p.status, sellingPrice:n(pick.selling_price)/10,
        epNext:n(p.ep_next), form:n(p.form), chance:p.chance_of_playing_next_round,
        minutes:n(p.minutes), elementType:p.element_type, clubId:p.team
      };
    }).filter(Boolean);

    const upcoming=(fixtures??[]).filter((f:any)=>f.event && f.event>=event.id && !f.finished).sort((a:any,b:any)=>a.event-b.event);
    const fixtureDifficulty=(clubId:number,horizon=3)=>{
      const fs=upcoming.filter((f:any)=>(f.team_h===clubId||f.team_a===clubId)).slice(0,horizon);
      if(!fs.length) return 3;
      return fs.reduce((s:number,f:any)=>s+n(f.team_h===clubId?f.team_h_difficulty:f.team_a_difficulty,3),0)/fs.length;
    };

    const clubCounts=new Map<number,number>();
    for(const p of squad as any[]) clubCounts.set(p.clubId,(clubCounts.get(p.clubId)||0)+1);
    const bank=n(entry.last_deadline_bank,0)/10;
    const freeTransfers=n(entry.transfers?.limit,1); // informational only; current API shape varies
    const teamValue=n(entry.last_deadline_value,n(entry.value,0))/10;

    const recs:any[]=[];
    for(const sold of squad as any[]){
      const budget=bank+sold.sellingPrice;
      const candidates=players.filter(p=>p.element_type===sold.elementType && p.id!==sold.id && p.status==="a" && n(p.now_cost)/10<=budget+0.001);
      for(const buy of candidates){
        const count=(clubCounts.get(buy.team)||0)-(sold.clubId===buy.team?1:0);
        if(count>=3) continue;
        const buyRisk=(100-n(buy.chance_of_playing_next_round,100))/100;
        const sellRisk=(100-n(sold.chance,100))/100;
        const fBuy=fixtureDifficulty(buy.team), fSell=fixtureDifficulty(sold.clubId);
        const epGain=n(buy.ep_next)-n(sold.ep_next);
        const formGain=n(buy.form)-n(sold.form);
        const fixtureGain=(fSell-fBuy)*0.18;
        const riskPenalty=(buyRisk-sellRisk)*0.45;
        const score=epGain+formGain*0.12+fixtureGain-riskPenalty;
        if(score<=0.10) continue;
        const confidence=Math.max(0.25,Math.min(0.9,0.45+Math.abs(score)*0.12-(buyRisk*0.25)));
        recs.push({
          sell:sold.name,buy:buy.web_name,position:sold.position,
          score:+score.toFixed(2),netExpectedGain:+score.toFixed(2),
          gain1:+score.toFixed(2),gain3:+(score*2.6).toFixed(2),gain6:+(score*4.4).toFixed(2),
          confidence:+confidence.toFixed(2),hitCost:freeTransfers>0?0:4,
          bankAfter:+(budget-n(buy.now_cost)/10).toFixed(1),
          expectedMinutesDifference:null,
          fixtureImpact:+fixtureGain.toFixed(2),
          risk:buyRisk>0.25?"availability risk":fBuy>4?"hard fixture run":"model uncertainty",
          reason:epGain>0.2?"higher next-GW expected points":fBuy<fSell?"better fixture run":"stronger current projection",
          horizon:"1/3/6 GW",
          dataTimestamp:new Date().toISOString()
        });
      }
    }
    recs.sort((a,b)=>b.score-a.score);
    const opportunities=recs.slice(0,5);
    return NextResponse.json({
      teamId,event:event.id,eventName:event.name,deadline:event.deadline_time,
      retrievedAt:new Date().toISOString(),bank,teamValue,freeTransfers,
      players:squad, opportunities,
      baseline:{action:"DO NOTHING",reason:"Roll is the comparison baseline; transfers are shown only when the estimated improvement clears the simple decision threshold."},
      model:{version:"live-web-0.1",method:"official FPL next-GW expected points + form + fixture difficulty + availability adjustment",calibration:"collecting",note:"This live web layer is not the recovered statistical backtest engine yet."}
    });
  } catch(e) {
    return NextResponse.json({error:e instanceof Error?e.message:"Unable to load FPL data."},{status:502});
  }
}
