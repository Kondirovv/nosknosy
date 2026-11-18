import catalog from './catalog.json' with {type:'json'};
import {validateOrder,digest} from './order-core.mjs';
import {rootCA} from './max-ca.ts';

const site='https://nos-k-nosu-shop.onrender.com';
const chat='-81360141302484';
const token=Deno.env.get('MAX_BOT_TOKEN');
const dbUrl=Deno.env.get('SUPABASE_URL');
const dbKey=Deno.env.get('SUPABASE_SERVICE_ROLE_KEY');
const client=Deno.createHttpClient({caCerts:[rootCA]});
async function db(path,method='GET',body=undefined){
  const r=await fetch(dbUrl+'/rest/v1/'+path,{method,headers:{apikey:dbKey,Authorization:'Bearer '+dbKey,'Content-Type':'application/json',Prefer:'return=representation'},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(10000)});
  if(!r.ok)throw Error('database');
  return r.status===204?null:await r.json();
}
Deno.serve(async req=>{
  const origin=req.headers.get('origin');
  const headers={'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store','Access-Control-Allow-Origin':site,'Vary':'Origin','Access-Control-Allow-Headers':'authorization,apikey,content-type','Access-Control-Allow-Methods':'POST,GET,OPTIONS'};
  const reply=(status,data)=>new Response(JSON.stringify(data),{status,headers});
  if(origin && origin!==site)return reply(403,{error:'Недопустимый источник запроса'});
  if(req.method==='OPTIONS')return new Response(null,{status:204,headers});
  if(req.method==='GET')return reply(200,{ready:!!(token&&dbUrl&&dbKey)});
  if(req.method!=='POST')return reply(405,{error:'Метод не поддерживается'});
  if(!token||!dbUrl||!dbKey)return reply(503,{error:'Автоматический приём заказов подключается. Напишите магазину в MAX или Telegram'});
  let order;
  try{
    if(Number(req.headers.get('content-length'))>16000)return reply(413,{error:'Слишком большой заказ'});
    const reader=req.body?.getReader(); if(!reader)return reply(400,{error:'Пустой заказ'});
    let raw='';const decoder=new TextDecoder();let length=0;
    while(true){const {done,value}=await reader.read();if(done)break;length+=value.length;if(length>16000){await reader.cancel();return reply(413,{error:'Слишком большой заказ'});}raw+=decoder.decode(value,{stream:true});}raw+=decoder.decode();
    order=validateOrder(JSON.parse(raw),catalog);
  }catch{return reply(400,{error:'Проверьте данные заказа и товары'});}
  const {normalized,total,text}=order;
  let claimed=false;
  try{
    const fingerprint=await digest(JSON.stringify(normalized));
    const rate=await digest(normalized.phone.replace(/\D/g,''));
    const claim=await db('rpc/claim_max_order','POST',{p_id:normalized.id,p_fingerprint:fingerprint,p_total:total,p_rate:rate});
    if(claim==='sent')return reply(200,{id:normalized.id,total});
    if(claim==='conflict')return reply(409,{error:'Этот номер относится к другому заказу. Свяжитесь с продавцом'});
    if(claim==='rate_limited')return reply(429,{error:'Слишком много попыток. Повторите через минуту'});
    if(claim!=='claimed')return reply(409,{error:'Результат предыдущей отправки уточняется. Не создавайте повторный заказ, свяжитесь с продавцом и назовите номер '+normalized.id});
    claimed=true;
    const r=await fetch('https://platform-api2.max.ru/messages?chat_id='+encodeURIComponent(chat),{client,method:'POST',headers:{Authorization:token,'Content-Type':'application/json'},body:JSON.stringify({text}),signal:AbortSignal.timeout(15000)});
    const data=await r.json();
    if(!r.ok||!data.message||data.success===false){
      const definitive=r.status>=400&&r.status<500;
      await db('max_order_deliveries?id=eq.'+normalized.id+'&status=eq.sending','PATCH',{status:definitive?'failed':'unknown',updated_at:new Date().toISOString()});
      claimed=false;
      console.error(JSON.stringify({stage:'max_response',status:r.status}));
      return reply(502,{error:definitive?'MAX не принял заказ. Повторите позже или свяжитесь с магазином':'Результат отправки уточняется. Свяжитесь с продавцом, номер '+normalized.id});
    }
    await db('max_order_deliveries?id=eq.'+normalized.id+'&status=eq.sending','PATCH',{status:'sent',updated_at:new Date().toISOString()});
    claimed=false;
    return reply(200,{id:normalized.id,total});
  }catch(e){
    console.error(JSON.stringify({stage:claimed?'delivery_uncertain':'database',code:e.name}));
    if(claimed){try{await db('max_order_deliveries?id=eq.'+normalized.id+'&status=eq.sending','PATCH',{status:'unknown',updated_at:new Date().toISOString()});}catch{}}
    return reply(503,{error:claimed?'Результат отправки уточняется. Свяжитесь с продавцом, номер '+normalized.id:'Приём временно недоступен. Попробуйте позже'});
  }
});

