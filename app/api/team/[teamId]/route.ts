import {NextResponse} from "next/server";
const FPL="https://fantasy.premierleague.com/api";
type Player={id:number;web_name:string;element_type:number;now_cost:number;team:number;status:string};
const positions:Record<number,string>={1:"GKP",2:"DEF",3:"MID",4:"FWD"};
export async function GET(_:Request,{params}:{params:Promise<{teamId:string}>}){
 const {teamId}=await params; const id=Number(teamId);
 if(!Number.isInteger(id)||id<=0)return NextResponse.json({error:"Invalid Team ID."},{status:400});
 try{
  const [bootstrap]=await Promise.all([fetch(FPL+"/bootstrap-static/",{cache:"no-store"})]);
  if(!bootstrap.ok)throw new Error("FPL data is currently unavailable.");
  const b=await bootstrap.json();
  const events=b.events as {id:number,is_current:boolean,is_next:boolean}[];
  const current=events.find(e=>e.is_current)?.id??events.find(e=>e.is_next)?.id??null;
  const eventForPicks=current??events[0]?.id;
  const picks=await fetch(FPL+"/entry/"+id+"/event/"+eventForPicks+"/picks/",{cache:"no-store"});
  if(!picks.ok)throw new Error("Team ID was not found or picks are unavailable.");
  const p=await picks.json(); const teams=new Map((b.teams as {id:number,name:string}[]).map(t=>[t.id,t.name]));
  const players=(p.picks||[]).map((pick:{element:number})=>{const x=(b.elements as Player[]).find(v=>v.id===pick.element);if(!x)return null;return{id:x.id,name:x.web_name,position:positions[x.element_type]||"—",price:x.now_cost/10,team:teams.get(x.team)||"—",status:x.status};}).filter(Boolean);
  return NextResponse.json({event:current,players,source:"FPL bootstrap-static + entry picks",retrievedAt:new Date().toISOString()},{headers:{"Cache-Control":"no-store"}});
 }catch(e){return NextResponse.json({error:e instanceof Error?e.message:"Unable to retrieve FPL data."},{status:502});}
}