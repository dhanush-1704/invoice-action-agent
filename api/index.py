import os, json, uuid, hashlib, re, threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import unquote

# NOTE: This implementation uses process memory as a development fallback.
# Production-grade durable atomic state must be backed by a transactional database.
LOCK = threading.RLock()
TASKS = {}
IDEMPOTENCY = {}
ACTIONS = {"settle_invoice","request_approval","hold_invoice","reject_duplicate","open_exception"}
MEDIA = "application/a2a+json"
BATCH_MEDIA = "application/vnd.ga5.invoice-claim-batch+json"

def now(): return datetime.now(timezone.utc).isoformat()
def canonical(x): return json.dumps(x, sort_keys=True, separators=(",",":"), ensure_ascii=False)
def err(code, message): return {"error":{"code":code,"message":message}}
def task_view(t):
    return {k:t[k] for k in ("id","contextId","status","history","artifacts","createdAt","updatedAt") if k in t}
def response(handler, status, obj, content_type=MEDIA):
    raw=json.dumps(obj,ensure_ascii=False).encode()
    handler.send_response(status); handler.send_header("Content-Type",content_type); handler.send_header("Content-Length",str(len(raw))); handler.end_headers(); handler.wfile.write(raw)
def body(handler):
    try:
        n=int(handler.headers.get("Content-Length","0"))
        if n>2_000_000: return None
        return json.loads(handler.rfile.read(n) or b"{}")
    except Exception: return None
def principal(handler):
    # The assignment does not provide a fixed token. Authenticate the exact
    # Bearer credential supplied by the caller and isolate tasks by its hash.
    auth=handler.headers.get("Authorization","")
    match=re.fullmatch(r"Bearer (\S+)", auth)
    if not match:
        return None
    return hashlib.sha256(match.group(1).encode()).hexdigest()
PROPOSALS_MEDIA = "application/vnd.ga5.invoice-action-proposals+json"
RECEIPTS_MEDIA = "application/vnd.ga5.invoice-action-receipts+json"


def artifact(data, mime=MEDIA):
    return {"artifactId":str(uuid.uuid4()),"parts":[{"type":"data","data":data,"mediaType":mime}]}
def refs_for(pkg):
    refs=pkg.get("evidenceRefs") or pkg.get("evidenceRefsUsed") or []
    if isinstance(refs,dict): refs=list(refs.keys())
    out=[]
    for item in refs:
        if isinstance(item,str) and item.strip(): out.append(item.strip())
        elif isinstance(item,dict):
            v=item.get("ref") or item.get("id") or item.get("evidenceRef")
            if isinstance(v,str) and v.strip(): out.append(v.strip())
    # Extract literal bracketed references already present in package text; never synthesize citations.
    if not out:
        raw=json.dumps(pkg,ensure_ascii=False,sort_keys=True)
        out=re.findall(r"\[[A-Za-z][A-Za-z0-9_.:/ -]{1,100}\]",raw)
    return list(dict.fromkeys(out))

def propose(pkg, batch_id, revision):
    facts=pkg.get("facts",{}) if isinstance(pkg.get("facts",{}),dict) else {}
    duplicate=bool(pkg.get("duplicate") or facts.get("duplicate") or pkg.get("duplicateOf"))
    missing=bool(pkg.get("missingFields") or facts.get("missingFields"))
    mismatch=bool(pkg.get("mismatch") or facts.get("mismatch"))
    if duplicate: action="reject_duplicate"
    elif missing: action="open_exception"
    elif mismatch: action="hold_invoice"
    elif (pkg.get("amountMinor") or facts.get("amountMinor") or pkg.get("amount")) and (pkg.get("approvalRequired") or facts.get("approvalRequired")): action="request_approval"
    elif pkg.get("settlementEligible", facts.get("settlementEligible", True)): action="settle_invoice"
    else: action="hold_invoice"
    refs=refs_for(pkg)[:3]
    source={**pkg, **facts}
    proposal_facts={k:source.get(k) for k in ("vendorName","invoiceNumber","amountMinor","currency") if source.get(k) is not None}
    rationale=(f"Proposed action {action} for package {pkg.get('packageId','unknown')} under policy revision {revision}. "
      f"The package indicators are duplicate={duplicate}, missingFields={bool(missing)}, mismatch={bool(mismatch)}. "
      f"Evidence references supplied with this package: {', '.join(refs) if refs else 'none supplied'}. "
      "Verify the cited source records before any financial side effect.")
    return {"packageId":pkg.get("packageId"),"actionId":str(uuid.uuid4()),"action":action,
      "facts":proposal_facts,"evidenceRefs":refs,"rationale":rationale}

def get_message_data(msg):
    # A2A HTTP+JSON parts carry mediaType and data; a "type":"data" field
    # is not required by the assignment's wire format.
    for p in msg.get("parts",[]):
        if isinstance(p,dict) and "data" in p and isinstance(p.get("mediaType"),str):
            return p.get("data"), p.get("mediaType")
    return None,None

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path=unquote(self.path.split("?",1)[0])
        if path=="/.well-known/agent-card.json":
            base=os.environ.get("PUBLIC_BASE_URL","https://invoice-action-agent-phi.vercel.app").rstrip("/")
            return response(self,200,{"name":"Invoice Action Agent","description":"A2A 1.0 invoice claim batch review agent","url":base,"version":"1.0.0","protocolVersion":"1.0","supportedInterfaces":[{"url":base,"protocolBinding":"HTTP+JSON","protocolVersion":"1.0"}],"capabilities":{"streaming":False,"pushNotifications":False},"defaultInputModes":[BATCH_MEDIA],"defaultOutputModes":["application/vnd.ga5.invoice-action-proposals+json","application/vnd.ga5.invoice-action-receipts+json"],"skills":[{"id":"invoice_action_agent","name":"Invoice action agent","description":"Reviews invoice claim packages, cites decisive evidence, and waits for explicit result continuations before recording accepted executions.","tags":["invoice","claims","evidence","approval","payments"],"inputModes":[BATCH_MEDIA],"outputModes":[PROPOSALS_MEDIA,RECEIPTS_MEDIA]}]})
        p=principal(self)
        if not p: return response(self,401,err("UNAUTHORIZED","Valid Bearer token required"))
        if self.headers.get("A2A-Version")!="1.0": return response(self,400,err("VERSION_REQUIRED","A2A-Version: 1.0 required"))
        if path=="/tasks":
            with LOCK: items=[task_view(t) for t in TASKS.values() if t["principal"]==p]
            return response(self,200,{"tasks":items})
        m=re.fullmatch(r"/tasks/([^/]+)",path)
        if m:
            with LOCK: t=TASKS.get(m.group(1))
            if not t or t["principal"]!=p: return response(self,404,err("TASK_NOT_FOUND","Task not found"))
            return response(self,200,task_view(t))
        return response(self,404,err("NOT_FOUND","Unknown endpoint"))
    def do_POST(self):
        path=unquote(self.path.split("?",1)[0])
        p=principal(self)
        if not p: return response(self,401,err("UNAUTHORIZED","Valid Bearer token required"))
        if self.headers.get("A2A-Version")!="1.0": return response(self,400,err("VERSION_REQUIRED","A2A-Version: 1.0 required"))
        if not self.headers.get("Content-Type","").split(";",1)[0].strip().lower()==MEDIA:
            return response(self,415,err("UNSUPPORTED_MEDIA_TYPE",f"Content-Type: {MEDIA} required"))
        data=body(self)
        if data is None: return response(self,400,err("INVALID_JSON","Request body must be valid JSON"))
        cancel=re.fullmatch(r"/tasks/([^/]+):cancel",path)
        if cancel:
            tid=cancel.group(1)
            with LOCK:
                t=TASKS.get(tid)
                if not t or t["principal"]!=p: return response(self,404,err("TASK_NOT_FOUND","Task not found"))
                if t["status"] in ("TASK_STATE_COMPLETED","TASK_STATE_CANCELED","TASK_STATE_FAILED"): return response(self,409,err("TERMINAL_TASK","Terminal tasks cannot be changed"))
                t["status"]="TASK_STATE_CANCELED"; t["updatedAt"]=now()
                t["history"].append({"role":"user","parts":[{"type":"text","text":"Task cancellation requested"}]})
                return response(self,200,task_view(t))
        if path!="/message:send": return response(self,404,err("NOT_FOUND","Unknown endpoint"))
        msg=data.get("message")
        if not isinstance(msg,dict) or not msg.get("messageId"): return response(self,400,err("INVALID_MESSAGE","message.messageId is required"))
        mid=str(msg["messageId"]); key=(p,mid); digest=hashlib.sha256(canonical(msg).encode()).hexdigest()
        with LOCK:
            if key in IDEMPOTENCY:
                old_hash,tid=IDEMPOTENCY[key]
                if old_hash!=digest: return response(self,409,err("IDEMPOTENCY_CONFLICT","messageId was already used with different message content"))
                return response(self,200,{"task":task_view(TASKS[tid])})
            t=None
            task_id=data.get("taskId") or data.get("id") or msg.get("taskId") or msg.get("id")
            if task_id:
                t=TASKS.get(str(task_id))
                if not t or t["principal"]!=p: return response(self,404,err("TASK_NOT_FOUND","Task not found"))
            if t:
                dat,mt=get_message_data(msg)
                if mt not in ("application/vnd.ga5.invoice-action-results+json", "application/vnd.ga5.invoice-action-result+json") or not isinstance(dat,dict): return response(self,400,err("INVALID_CONTINUATION","Continuation must contain a supported A2A data part"))
                if t["status"]!="TASK_STATE_INPUT_REQUIRED": return response(self,409,err("TASK_NOT_AWAITING_INPUT","Task is not awaiting input"))\n                if not isinstance(dat.get("batchId"),str) or not dat.get("batchId"): return response(self,400,err("INVALID_RESULTS","batchId is required in the continuation results part"))
                continuation_context=dat.get("contextId") or msg.get("contextId")\n                if continuation_context!=t["contextId"] or dat.get("batchId")!=t["batchId"]: return response(self,409,err("CONTEXT_MISMATCH","Continuation context or batch does not match"))
                outcomes=dat.get("results",dat.get("outcomes",[]))
                if not isinstance(outcomes,list): return response(self,400,err("INVALID_RESULTS","results must be an array"))
                executions=[]
                proposals=t.get("proposals",[])
                for outcome in outcomes:
                    if not isinstance(outcome,dict): return response(self,400,err("INVALID_RESULT","Each result must be an object"))
                    proposal=next((x for x in proposals if x["packageId"]==outcome.get("packageId") and x["actionId"]==outcome.get("actionId")),None)
                    if not proposal or outcome.get("action")!=proposal["action"]: return response(self,409,err("PROPOSAL_MISMATCH","Result does not match a proposed package/action"))
                    if outcome.get("outcome") not in ("ACCEPTED","REJECTED"):
                        return response(self,400,err("INVALID_OUTCOME","outcome must be ACCEPTED or REJECTED"))
                    if outcome.get("outcome")=="ACCEPTED":
                        nonce=outcome.get("receiptNonce")
                        if not isinstance(nonce,str) or not nonce: return response(self,400,err("RECEIPT_NONCE_REQUIRED","Accepted outcomes require receiptNonce"))
                        executions.append({"packageId":proposal["packageId"],"actionId":proposal["actionId"],"action":proposal["action"],"receiptNonce":nonce,"facts":proposal["facts"],"evidenceRefs":proposal["evidenceRefs"]})
                t["history"].append(msg); t["artifacts"].append(artifact({"batchId":t["batchId"],"executions":executions},RECEIPTS_MEDIA))
                t["status"]="TASK_STATE_COMPLETED"; t["updatedAt"]=now(); IDEMPOTENCY[key]=(digest,t["id"])
                return response(self,200,{"task":task_view(t)})
            dat,mt=get_message_data(msg)
            if mt!=BATCH_MEDIA or not isinstance(dat,dict): return response(self,400,err("INVALID_BATCH","A data part with the invoice-claim batch media type is required"))
            packages=dat.get("packages")
            if not isinstance(packages,list) or not packages or any(not isinstance(pkg,dict) for pkg in packages): return response(self,400,err("INVALID_BATCH","packages must be a non-empty array of objects"))
            bid=dat.get("batchId"); rev=dat.get("policyRevision")
            if not bid or rev is None: return response(self,400,err("INVALID_BATCH","batchId and policyRevision are required"))
            proposals=[propose(pkg,bid,rev) for pkg in packages]
            tid=str(uuid.uuid4()); context=msg.get("contextId") or str(uuid.uuid4())
            t={"id":tid,"contextId":context,"principal":p,"status":"TASK_STATE_INPUT_REQUIRED","batchId":bid,"policyRevision":rev,"proposals":proposals,"history":[msg],"artifacts":[artifact({"batchId":bid,"proposals":proposals},PROPOSALS_MEDIA)],"createdAt":now(),"updatedAt":now()}
            TASKS[tid]=t; IDEMPOTENCY[key]=(digest,tid)
            return response(self,200,{"task":task_view(t)})
    def log_message(self,*args): pass
