#!/usr/bin/env python3
"""Local-only API for APK Maker Studio. GitHub credentials stay in memory."""
from __future__ import annotations
import base64, hashlib, json, mimetypes, os, re, threading, time, uuid, zipfile
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
ARTIFACTS = DATA / "artifacts"
STATE_PATH = DATA / "state.json"
MAX_UPLOAD = 65 * 1024 * 1024
API = "https://api.github.com"
_lock = threading.RLock()
_engine = {"repo": os.environ.get("BUILD_GH_REPO", ""), "token": os.environ.get("BUILD_GH_TOKEN", ""), "branch": os.environ.get("BUILD_GH_BRANCH", "main")}
_builds: dict[str, dict] = {}
DATA.mkdir(exist_ok=True); ARTIFACTS.mkdir(exist_ok=True)

def load_state():
    global _builds
    try:
        state=json.loads(STATE_PATH.read_text("utf-8"))
        _builds={str(x["id"]):x for x in state.get("builds",[]) if "id" in x}
    except (OSError, ValueError, TypeError):
        _builds={}
def save_state():
    tmp=STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"builds":list(_builds.values())},ensure_ascii=False,indent=2),"utf-8")
    tmp.replace(STATE_PATH)
load_state()

def gh(method: str, path: str, payload=None, token=None, accept="application/vnd.github+json", timeout=45):
    token=token or _engine.get("token")
    if not token: raise RuntimeError("GitHub token is not configured")
    data=None if payload is None else json.dumps(payload).encode()
    req=Request(API+path,data=data,method=method,headers={"Accept":accept,"Authorization":"Bearer "+token,"X-GitHub-Api-Version":"2026-03-10","User-Agent":"APK-Maker-Studio-Local"})
    try:
        with urlopen(req,timeout=timeout) as res:
            body=res.read()
            if "json" in res.headers.get("Content-Type",""): return json.loads(body or b"{}")
            return body
    except HTTPError as e:
        text=e.read(2000).decode("utf-8","replace")
        try: detail=json.loads(text).get("message",text)
        except ValueError: detail=text
        raise RuntimeError(f"GitHub {e.code}: {detail}") from e
    except URLError as e: raise RuntimeError(f"GitHub connection failed: {e.reason}") from e

def repo_path(repo):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",repo or ""):
        raise ValueError("Repository must be in owner/name format")
    return "/repos/"+quote(repo,safe="/")

def put_workflow(repo, token):
    path=".github/workflows/build-apk.yml"
    api_path=repo_path(repo)+"/contents/"+quote(path,safe="/")
    try: old=gh("GET",api_path,token=token)
    except RuntimeError as e:
        if "404" not in str(e): raise
        old={}
    content=(ROOT/"build-apk.yml").read_text("utf-8")
    body={"message":"Add APK Maker Studio Android build workflow","content":base64.b64encode(content.encode()).decode()}
    if old.get("sha"): body["sha"]=old["sha"]
    gh("PUT",api_path,body,token=token)

def detect_type(zip_bytes: bytes) -> str:
    try:
        with zipfile.ZipFile(__import__('io').BytesIO(zip_bytes)) as z:
            names=[n.lower().lstrip("./") for n in z.namelist() if not n.endswith("/")]
        if any(n.endswith("pubspec.yaml") for n in names): return "flutter"
        if any(n.endswith("settings.gradle") or n.endswith("settings.gradle.kts") for n in names): return "android"
        return "web"
    except zipfile.BadZipFile: raise ValueError("Uploaded file is not a valid ZIP archive")

def fetch_public_archive(owner: str, repo: str, ref: str) -> bytes:
    url=f"{API}/repos/{quote(owner,safe='')}/{quote(repo,safe='')}/zipball/{quote(ref,safe='')}"
    req=Request(url,headers={"Accept":"application/vnd.github+json","User-Agent":"APK-Maker-Studio-Local"})
    chunks=[]; total=0
    with urlopen(req,timeout=120) as res:
        length=res.headers.get("Content-Length")
        if length and int(length)>MAX_UPLOAD: raise ValueError("The GitHub project archive exceeds 65 MB")
        while True:
            part=res.read(1024*1024)
            if not part: break
            total+=len(part)
            if total>MAX_UPLOAD: raise ValueError("The GitHub project archive exceeds 65 MB")
            chunks.append(part)
    return b"".join(chunks)

def upload_source(repo, build_id, source_bytes):
    p=repo_path(repo)+"/contents/sources/"+build_id+".zip"
    body={"message":"Add private build source "+build_id,"content":base64.b64encode(source_bytes).decode()}
    gh("PUT",p,body,timeout=120)

def dispatch(repo, inputs):
    gh("POST",repo_path(repo)+"/actions/workflows/build-apk.yml/dispatches",{"ref":_engine.get("branch") or "main","inputs":inputs})

def refresh_build(b):
    repo=b.get("repo") or _engine.get("repo")
    if not repo or not _engine.get("token"): return b
    try:
        runs=gh("GET",repo_path(repo)+"/actions/workflows/build-apk.yml/runs?per_page=100").get("workflow_runs",[])
        needle="APKMS-"+b["id"]
        run=next((r for r in runs if r.get("name","").startswith(needle)),None)
        if not run: return b
        b["run_id"]=run["id"]
        if run.get("status")!="completed": b["status"]="running"; return b
        if run.get("conclusion")!="success":
            b["status"]="failed"; b["error"]="GitHub Actions build failed. Open the run log in the private build repository for details."
            try:
                import io
                logs=gh("GET",repo_path(repo)+f"/actions/runs/{run['id']}/logs",timeout=90)
                with zipfile.ZipFile(io.BytesIO(logs)) as archive:
                    texts=[archive.read(n).decode("utf-8","replace") for n in archive.namelist() if n.lower().endswith((".txt",".log"))]
                if texts:
                    tail="\n".join(texts[-1].splitlines()[-18:])[-2400:]
                    if tail.strip(): b["error"]=tail
            except Exception: pass
            return b
        artifacts=gh("GET",repo_path(repo)+f"/actions/runs/{run['id']}/artifacts")
        artifact=next((a for a in artifacts.get("artifacts",[]) if a.get("name")=="android-package" and not a.get("expired")),None)
        if not artifact: b["status"]="failed"; b["error"]="Verified build artifact was not found."; return b
        archive=gh("GET",repo_path(repo)+f"/actions/artifacts/{artifact['id']}/zip",accept="application/vnd.github+json",timeout=120)
        import io
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            names=z.namelist()
            verify_name=next((n for n in names if n.endswith("verification.json")),None)
            artifact_name=next((n for n in names if n.lower().endswith((".apk",".aab"))),None)
            sums=next((n for n in names if n.endswith("sha256.txt")),None)
            if not verify_name or not artifact_name or not sums: raise RuntimeError("Build archive is missing verification data")
            verify=json.loads(z.read(verify_name))
            content=z.read(artifact_name)
            expected=z.read(sums).decode().split()[0]
            actual=hashlib.sha256(content).hexdigest()
            if not verify.get("verified") or actual!=expected or actual!=verify.get("sha256"):
                raise RuntimeError("Artifact signature or checksum verification failed")
            target=ARTIFACTS/(b["id"]+Path(artifact_name).suffix.lower())
            target.write_bytes(content)
            b["artifact_path"]=target.name; b["sha256"]=actual; b["verified"]=True; b["status"]="success"
    except Exception as e:
        b["status"]="failed"; b["error"]=str(e)
    return b

class Handler(BaseHTTPRequestHandler):
    server_version="APKMakerLocal/1.0"
    def log_message(self, fmt, *args): print("%s - %s"%(self.address_string(),fmt%args))
    def json(self, status, body):
        raw=json.dumps(body,ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(raw))); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(raw)
    def read_json(self):
        n=int(self.headers.get("Content-Length","0"))
        if n>1024*1024: raise ValueError("Request is too large")
        return json.loads(self.rfile.read(n) or b"{}")
    def public_build(self, b):
        created=b.get("created_at",time.time())
        result={"id":b["id"],"projectId":b.get("project_id"),"name":b.get("name","App"),"icon":b.get("icon","✦"),"format":b.get("format","apk"),"status":b.get("status","pending"),"verified":bool(b.get("verified")),"createdAt":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime(created)),"sha256":b.get("sha256"),"error":b.get("error")}
        if b.get("status")=="success" and b.get("verified"): result["downloadUrl"]=f"/api/artifacts/{b['id']}"
        return result
    def do_GET(self):
        path=urlparse(self.path).path
        if path=="/api/state":
            with _lock:
                for b in _builds.values():
                    if b.get("status") in ("running","pending"): refresh_build(b)
                save_state(); public=[self.public_build(b) for b in _builds.values()]
                self.json(200,{"engine":{"connected":bool(_engine.get("token") and _engine.get("repo")),"repo":_engine.get("repo","")},"builds":public})
            return
        if path.startswith("/api/builds/"):
            bid=path.rsplit("/",1)[-1]
            with _lock:
                b=_builds.get(bid)
                if not b: return self.json(404,{"error":"Build not found"})
                if b.get("status") in ("running","pending"): refresh_build(b); save_state()
                return self.json(200,self.public_build(b)|{"download_url":f"/api/artifacts/{bid}" if b.get("status")=="success" and b.get("verified") else None})
        if path.startswith("/api/artifacts/"):
            bid=path.rsplit("/",1)[-1]
            b=_builds.get(bid,{})
            fp=ARTIFACTS/(b.get("artifact_path") or "")
            if not b.get("verified") or not fp.is_file(): return self.json(404,{"error":"No verified build artifact"})
            data=fp.read_bytes(); self.send_response(200); self.send_header("Content-Type","application/vnd.android.package-archive"); self.send_header("Content-Length",str(len(data))); self.send_header("Content-Disposition",f'attachment; filename="{b["name"]}.{b["format"]}"'); self.end_headers(); self.wfile.write(data); return
        static={"/":"index.html","/index.html":"index.html","/build-apk.yml":"build-apk.yml","/README.md":"README.md"}
        name=static.get(path)
        if not name: return self.json(404,{"error":"Not found"})
        fp=ROOT/name
        try: data=fp.read_bytes()
        except OSError: return self.json(404,{"error":"File not found"})
        typ=mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
        if name.endswith(".yml"): typ="text/yaml; charset=utf-8"
        if name.endswith(".md"): typ="text/markdown; charset=utf-8"
        self.send_response(200); self.send_header("Content-Type",typ); self.send_header("Content-Length",str(len(data))); self.send_header("X-Content-Type-Options","nosniff"); self.end_headers(); self.wfile.write(data)
    def do_POST(self):
        path=urlparse(self.path).path
        try:
            if path in ("/api/engine/setup","/api/engine/connect"):
                data=self.read_json(); token=(data.get("token") or "").strip()
                if len(token)<20: return self.json(400,{"error":"Enter a valid GitHub token"})
                user=gh("GET","/user",token=token)
                if path.endswith("setup"):
                    repo=f"{user['login']}/apk-maker-builds"
                    try:
                        obj=gh("GET",repo_path(repo),token=token)
                        if not obj.get("private"): return self.json(409,{"error":"Repository exists and is public. Use a private repository name."})
                    except RuntimeError as e:
                        if "404" not in str(e): raise
                        obj=gh("POST","/user/repos",{"name":"apk-maker-builds","private":True,"auto_init":True,"description":"Private Android build runner for APK Maker Studio"},token=token)
                    if obj.get("owner",{}).get("login","").lower()!=user["login"].lower(): raise RuntimeError("Repository owner mismatch")
                    put_workflow(repo,token)
                else:
                    repo=(data.get("repo") or "").strip()
                    obj=gh("GET",repo_path(repo),token=token)
                    if not obj.get("private"): return self.json(400,{"error":"Build repository must be private"})
                    wf=gh("GET",repo_path(repo)+"/contents/.github/workflows/build-apk.yml",token=token)
                    if not wf: raise RuntimeError("Build workflow is missing from this repository")
                _engine["repo"]=repo; _engine["token"]=token; _engine["branch"]=obj.get("default_branch") or "main"
                return self.json(200,{"connected":True,"repository":repo})
            if path=="/api/builds": return self.create_build()
            return self.json(404,{"error":"Not found"})
        except ValueError as e: return self.json(400,{"error":str(e)})
        except Exception as e: return self.json(502,{"error":str(e)[:1000]})
    def create_build(self):
        if not _engine.get("token") or not _engine.get("repo"): return self.json(409,{"error":"Build engine is not connected"})
        length=int(self.headers.get("Content-Length","0"))
        if length>MAX_UPLOAD+2*1024*1024: return self.json(413,{"error":"Upload must be under 65 MB"})
        raw=self.rfile.read(length)
        ctype=self.headers.get("Content-Type","")
        msg=BytesParser(policy=default).parsebytes((f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n").encode()+raw)
        parts={}
        for part in msg.iter_parts():
            name=part.get_param("name",header="content-disposition")
            if not name: continue
            parts[name]=(part.get_filename(),part.get_payload(decode=True) or b"")
        try: p=json.loads(parts["project"][1])
        except Exception: return self.json(400,{"error":"Project details are missing"})
        source=parts.get("source",(None,b""))[1]
        source_ref=""
        if p.get("sourceType")=="zip":
            if source:
                if len(source)>MAX_UPLOAD: return self.json(413,{"error":"Upload must be under 65 MB"})
                project_type=detect_type(source); source_ref="repo:sources/"+uuid.uuid4().hex+".zip"
            elif p.get("sourceBuildId"):
                prior=_builds.get(str(p["sourceBuildId"]))
                if not prior or prior.get("project_id")!=p.get("id") or not prior.get("source_path"):
                    return self.json(400,{"error":"The saved source ZIP is unavailable. Upload the project again."})
                project_type=p.get("projectType") or prior.get("project_type") or "web"
                source_ref="repo:"+prior["source_path"]
            else: return self.json(400,{"error":"Project ZIP is missing"})
        else:
            match=re.fullmatch(r"https://github\.com/([\w.-]+)/([\w.-]+)/?",p.get("repo", ""))
            if not match: return self.json(400,{"error":"Enter a public GitHub repository URL"})
            p["repo_owner"],p["repo_name"]=match.groups()
            source_repo=f"{p['repo_owner']}/{p['repo_name']}"
            repo_info=gh("GET",repo_path(source_repo))
            if repo_info.get("private"): return self.json(400,{"error":"The project repository must be public"})
            archive=fetch_public_archive(p["repo_owner"],p["repo_name"],repo_info.get("default_branch") or "main")
            project_type=detect_type(archive)
        bid=uuid.uuid4().hex
        if source:
            source_ref="repo:sources/"+bid+".zip"
            upload_source(_engine["repo"],bid,source)
        elif p.get("repo"):
            p["source_url"]=f"https://api.github.com/repos/{p['repo_owner']}/{p['repo_name']}/zipball/{quote(repo_info.get('default_branch') or 'main',safe='')}"
        icon_data=p.get("iconData","")
        icon_raw=""
        if isinstance(icon_data,str) and icon_data.startswith("data:image/") and "," in icon_data:
            icon_raw=icon_data.split(",",1)[1]
            if len(icon_raw)>40_000: icon_raw=""
        app_name=str(p.get("name","App")).strip(); package_name=str(p.get("packageName","com.apkmaker.app")); output_format=p.get("format","apk")
        if not app_name or len(app_name)>48: return self.json(400,{"error":"App name must contain 1 to 48 characters"})
        if not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,}",package_name): return self.json(400,{"error":"Invalid Android package identifier"})
        if output_format not in ("apk","aab"): return self.json(400,{"error":"Output format must be apk or aab"})
        inputs={"build_id":bid,"app_name":app_name,"package_name":package_name,"output_format":output_format,"project_type":project_type,"source_url":p.get("source_url") or source_ref,"icon_data":icon_raw}
        dispatch(_engine["repo"],inputs)
        b={"id":bid,"project_id":p.get("id"),"name":inputs["app_name"],"icon":p.get("icon","✦"),"format":inputs["output_format"],"status":"running","verified":False,"created_at":time.time(),"repo":_engine["repo"],"project_type":project_type,"source_path":"sources/"+bid+".zip" if source else (source_ref[5:] if source_ref.startswith("repo:") else "")}
        with _lock: _builds[bid]=b; save_state()
        return self.json(202,{"build_id":bid,"status":"running","project_type":project_type})

def main():
    host=os.environ.get("APKMS_HOST","127.0.0.1"); port=int(os.environ.get("APKMS_PORT","8787"))
    if host not in ("127.0.0.1","localhost","::1"):
        raise SystemExit("For safety, the local server only binds to loopback. Do not expose it to the public internet.")
    print(f"APK Maker Studio is ready at http://127.0.0.1:{port}")
    ThreadingHTTPServer((host,port),Handler).serve_forever()
if __name__=="__main__": main()

