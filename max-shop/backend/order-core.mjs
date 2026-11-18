export function validateOrder(o, catalog) {
  if (!o || !/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/i.test(o.id) || typeof o.name !== 'string' || !o.name.trim() || o.name.length > 80 || /[\r\n]/.test(o.name) || typeof o.phone !== 'string' || !/^\+?[\d ()-]{7,25}$/.test(o.phone) || !['Самовывоз','Доставка (условия уточнит продавец)'].includes(o.method) || !Array.isArray(o.items) || !o.items.length || o.items.length > 50) throw Error('Проверьте данные заказа');
  const products = new Map();
  for (const p of catalog.flat()) for (const v of p.variants || [p]) products.set(p.id+'-'+v.unit, {name:p.name, unit:v.unit, price:v.price});
  let total=0; const lines=[]; const keys=new Set();
  const items=o.items.map(i=>{
    const p=products.get(i?.key);
    if (!p || keys.has(i.key) || !Number.isInteger(i.qty) || i.qty<1 || i.qty>999) throw Error('Проверьте товары и количество');
    keys.add(i.key); total+=p.price*i.qty;
    lines.push(`${p.name}, ${p.unit} × ${i.qty} — ${p.price*i.qty} ₽`);
    return {key:i.key,qty:i.qty};
  }).sort((a,b)=>a.key.localeCompare(b.key));
  const normalized={id:o.id,name:o.name.trim(),phone:o.phone.trim(),method:o.method,items};
  const text=`Новый заказ ${o.id}\n${normalized.name}\nТелефон: ${normalized.phone}\nПолучение: ${o.method}\n\n${lines.join('\n')}\nИтого: ${total} ₽`;
  if(text.length>3900 || total>2147483647) throw Error('Разделите большой заказ');
  return {normalized,total,text};
}

export async function digest(value) {
  const bytes=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(value));
  return Array.from(new Uint8Array(bytes),b=>b.toString(16).padStart(2,'0')).join('');
}

