export class ApiError extends Error { status:number; constructor(message:string,status=0){super(message);this.name="ApiError";this.status=status;} }
export async function postQuery(query:string, signal?:AbortSignal){
 const base=(import.meta.env.VITE_API_BASE_URL||"http://127.0.0.1:8000").replace(/\/$/,"");
 let res:Response; try{res=await fetch(`${base}/query`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({query}),signal});}
 catch(e){if(e instanceof DOMException && e.name==="AbortError") throw e; throw new ApiError("Could not reach the Vana API.");}
 const data=await res.json().catch(()=>null);
 if(!res.ok) throw new ApiError(data?.detail?.message||data?.error?.message||`API request failed (${res.status}).`,res.status);
 return data;
}