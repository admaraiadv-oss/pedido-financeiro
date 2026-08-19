import os
import io
import json
import logging
import httpx
import zipfile
from datetime import datetime
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse, HTMLResponse
from typing import List, Optional
import fitz
from PIL import Image
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="../frontend"), name="static")

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

def get_oauth_config():
    return {
        "web": {
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [os.environ["OAUTH_REDIRECT_URI"]],
        }
    }

def save_token_to_render(token_json: str):
    """Persist refreshed token back to Render environment variable."""
    try:
        render_api_key = os.environ.get("RENDER_API_KEY")
        service_id = os.environ.get("RENDER_SERVICE_ID")
        if not render_api_key or not service_id:
            logger.warning("RENDER_API_KEY ou RENDER_SERVICE_ID não configurados.")
            return
        headers = {
            "Authorization": f"Bearer {render_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        # Get current env vars
        url = f"https://api.render.com/v1/services/{service_id}/env-vars"
        r = httpx.get(url, headers=headers, timeout=15)
        logger.info(f"Render GET env-vars: {r.status_code}")
        if r.status_code != 200:
            logger.error(f"Erro ao buscar env vars: {r.text[:300]}")
            return
        env_vars = r.json()
        # Build updated list
        updated = []
        found = False
        for ev in env_vars:
            key = ev.get("envVar", {}).get("key") or ev.get("key", "")
            val = ev.get("envVar", {}).get("value") or ev.get("value", "")
            if key == "GOOGLE_TOKEN_JSON":
                updated.append({"key": "GOOGLE_TOKEN_JSON", "value": token_json})
                found = True
            elif key:
                updated.append({"key": key, "value": val})
        if not found:
            updated.append({"key": "GOOGLE_TOKEN_JSON", "value": token_json})
        r2 = httpx.put(url, headers=headers, json=updated, timeout=15)
        logger.info(f"Render PUT env-vars: {r2.status_code} - {r2.text[:200]}")
        if r2.status_code in (200, 201):
            logger.info("Token salvo no Render com sucesso.")
        else:
            logger.error(f"Erro ao salvar token: {r2.text[:300]}")
    except Exception as e:
        logger.error(f"Erro ao persistir token: {e}")

def get_credentials() -> Credentials:
    token_data = os.environ.get("GOOGLE_TOKEN_JSON")
    if not token_data:
        raise HTTPException(401, "Sistema não autorizado. Acesse /auth para autorizar.")
    creds = Credentials.from_authorized_user_info(json.loads(token_data), SCOPES)
    if creds.expired and creds.refresh_token:
        logger.info("Token expirado, renovando...")
        creds.refresh(GoogleRequest())
        new_token = creds.to_json()
        os.environ["GOOGLE_TOKEN_JSON"] = new_token
        save_token_to_render(new_token)
    return creds

def get_services():
    creds = get_credentials()
    drive = build("drive", "v3", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)
    return drive, sheets

# ── Auth endpoints ────────────────────────────────────────────────────────────

@app.get("/auth")
def auth():
    flow = Flow.from_client_config(get_oauth_config(), scopes=SCOPES)
    flow.redirect_uri = os.environ["OAUTH_REDIRECT_URI"]
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    return RedirectResponse(auth_url)

@app.get("/oauth/callback")
async def oauth_callback(request: Request):
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(400, "Código de autorização não encontrado.")
    flow = Flow.from_client_config(get_oauth_config(), scopes=SCOPES)
    flow.redirect_uri = os.environ["OAUTH_REDIRECT_URI"]
    flow.fetch_token(code=code)
    creds = flow.credentials
    token_json = creds.to_json()
    os.environ["GOOGLE_TOKEN_JSON"] = token_json
    save_token_to_render(token_json)
    return HTMLResponse("""
    <html><body style="font-family:sans-serif;padding:40px;background:#f5f5f5;text-align:center">
    <h2>✅ Autorização concluída!</h2>
    <p>O sistema está autorizado e pronto para uso.</p>
    <br><a href="/" style="background:#1B2D4F;color:white;padding:12px 24px;border-radius:4px;text-decoration:none">Ir ao formulário</a>
    </body></html>
    """)

# ── PDF utils ────────────────────────────────────────────────────────────────

def image_bytes_to_pdf_bytes(image_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PDF")
    return buf.getvalue()

def to_pdf_bytes(content: bytes, filename: str) -> bytes:
    ext = filename.rsplit(".", 1)[-1].lower()
    return content if ext == "pdf" else image_bytes_to_pdf_bytes(content)

def merge_pdfs(pdf_list: list) -> bytes:
    merged = fitz.open()
    for pb in pdf_list:
        src = fitz.open(stream=pb, filetype="pdf")
        merged.insert_pdf(src)
        src.close()
    buf = io.BytesIO()
    merged.save(buf)
    merged.close()
    return buf.getvalue()

# ── Drive helpers ────────────────────────────────────────────────────────────

def upload_to_drive(drive, content: bytes, filename: str, folder_id: str):
    media = MediaIoBaseUpload(io.BytesIO(content), mimetype="application/pdf", resumable=False)
    file_meta = {"name": filename, "parents": [folder_id]}
    f = drive.files().create(body=file_meta, media_body=media, fields="id,webViewLink").execute()
    return f.get("id", ""), f.get("webViewLink", "")

def download_from_drive(drive, file_id: str) -> bytes:
    request = drive.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()

def delete_from_drive(drive, file_id: str):
    drive.files().delete(fileId=file_id).execute()

def list_pending(drive) -> list:
    folder_id = os.environ["DRIVE_FOLDER_PENDENTES"]
    results = drive.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id, name, webViewLink)",
        orderBy="name"
    ).execute()
    return results.get("files", [])

def get_valor_by_drive_id(sheets, drive_ids: list) -> dict:
    """Return dict of {drive_id: valor} by looking up column J in Sheets."""
    if not drive_ids:
        return {}
    spreadsheet_id = os.environ["SHEETS_ID"]
    sheet_name = os.environ.get("SHEET_NAME", "1")
    result = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A:J"
    ).execute()
    values = result.get("values", [])
    mapping = {}
    for row in values[1:]:
        if len(row) > 9 and row[9] in drive_ids:
            valor = row[3] if len(row) > 3 else ""
            mapping[row[9]] = valor
    return mapping

# ── Sheets helpers ───────────────────────────────────────────────────────────

def get_next_anexo_number(sheets) -> int:
    spreadsheet_id = os.environ["SHEETS_ID"]
    sheet_name = os.environ.get("SHEET_NAME", "1")
    result = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!E:E"
    ).execute()
    values = result.get("values", [])
    max_num = 0
    for row in values[1:]:
        if row:
            try:
                n = int(str(row[0]).strip())
                if n > max_num:
                    max_num = n
            except (ValueError, TypeError):
                pass
    return max_num + 1

def append_row_sheets(sheets, row_data: list):
    spreadsheet_id = os.environ["SHEETS_ID"]
    sheet_name = os.environ.get("SHEET_NAME", "1")
    sheets.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row_data]}
    ).execute()

def update_anexo_in_sheets(sheets, file_drive_id: str, numero: int):
    spreadsheet_id = os.environ["SHEETS_ID"]
    sheet_name = os.environ.get("SHEET_NAME", "1")
    result = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A:J"
    ).execute()
    values = result.get("values", [])
    for i, row in enumerate(values):
        if len(row) > 9 and row[9] == file_drive_id:
            row_num = i + 1
            sheets.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{sheet_name}!E{row_num}",
                valueInputOption="USER_ENTERED",
                body={"values": [[str(numero)]]}
            ).execute()
            sheets.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{sheet_name}!J{row_num}",
                valueInputOption="USER_ENTERED",
                body={"values": [[""]]}
            ).execute()
            return True
    return False

# ── Main endpoints ────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return FileResponse("../frontend/index.html")

@app.get("/comprovante")
def comprovante_page():
    return FileResponse("../frontend/comprovante.html")

@app.post("/submit")
async def submit(
    tipo: str = Form(...),
    advogado: str = Form(...),
    cliente: str = Form(...),
    processo: Optional[str] = Form(None),
    descricao: str = Form(...),
    valor: str = Form(...),
    obs: Optional[str] = Form(None),
    arquivos: List[UploadFile] = File(default=[]),
):
    try:
        drive, sheets = get_services()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Erro de autenticação Google: {e}")

    descricao_completa = descricao
    if processo and processo.strip():
        descricao_completa += f" {processo.strip()}"

    descricao_formula = f"{descricao_completa} - {cliente} - {advogado}"
    data_hoje = datetime.now().strftime("%d/%m/%Y")

    def safe(s: str, limit=40) -> str:
        return s.replace("/", "-").replace("\\", "-")[:limit]

    folder_pendentes = os.environ["DRIVE_FOLDER_PENDENTES"]
    valid_files = [f for f in arquivos if f and f.filename]
    has_file = bool(valid_files)
    file_drive_id = ""

    if has_file:
        pdfs = []
        for f in valid_files:
            content = await f.read()
            pdfs.append(to_pdf_bytes(content, f.filename))
        merged = merge_pdfs(pdfs) if len(pdfs) > 1 else pdfs[0]
        filename = f"PENDENTE {safe(descricao_completa)} - {safe(cliente)} - {safe(advogado)}.pdf"
        try:
            file_drive_id, _ = upload_to_drive(drive, merged, filename, folder_pendentes)
        except Exception as e:
            raise HTTPException(500, f"Erro ao salvar no Drive: {e}")

    row = [
        data_hoje, cliente, descricao_formula,
        valor.replace(",", "."),
        "pendente" if has_file else "x",
        advogado, descricao_completa, "Não", obs or "", file_drive_id,
    ]

    try:
        append_row_sheets(sheets, row)
    except Exception as e:
        raise HTTPException(500, f"Erro ao gravar no Sheets: {e}")

    return {"ok": True, "message": "Pedido registrado com sucesso!"}

@app.get("/pendentes")
async def get_pendentes():
    try:
        drive, sheets = get_services()
        files = list_pending(drive)
        if files:
            drive_ids = [f["id"] for f in files]
            valor_map = get_valor_by_drive_id(sheets, drive_ids)
            for f in files:
                f["valor"] = valor_map.get(f["id"], "")
                # Convert webViewLink to embeddable preview link
                wvl = f.get("webViewLink", "")
                if wvl:
                    file_id = f["id"]
                    f["previewLink"] = f"https://drive.google.com/file/d/{file_id}/preview"
                else:
                    f["previewLink"] = ""
        return {"files": files}
    except Exception as e:
        raise HTTPException(500, f"Erro ao listar pendentes: {e}")

@app.post("/anexar-comprovante")
async def anexar_comprovante(
    file_id: str = Form(...),
    file_name: str = Form(...),
    comprovante: UploadFile = File(...),
):
    try:
        drive, sheets = get_services()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Erro de autenticação Google: {e}")

    folder_comprovantes = os.environ["DRIVE_FOLDER_COMPROVANTES"]

    try:
        next_num = get_next_anexo_number(sheets)
        original_pdf = download_from_drive(drive, file_id)
        comp_content = await comprovante.read()
        comp_pdf = to_pdf_bytes(comp_content, comprovante.filename)
        merged = merge_pdfs([original_pdf, comp_pdf])
        base_name = file_name.replace("PENDENTE ", "")
        final_name = f"{next_num} {base_name}"
        new_file_id, _ = upload_to_drive(drive, merged, final_name, folder_comprovantes)
        delete_from_drive(drive, file_id)
        update_anexo_in_sheets(sheets, file_id, next_num)
    except Exception as e:
        logger.error(f"Erro ao anexar comprovante: {e}")
        raise HTTPException(500, f"Erro: {e}")

    return {"ok": True, "numero": next_num, "filename": final_name, "drive_id": new_file_id}



@app.get("/buscar-relatorio")
async def buscar_relatorio(cliente: str, data_inicio: str, data_fim: str):
    try:
        drive, sheets = get_services()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Erro de autenticação: {e}")

    spreadsheet_id = os.environ["SHEETS_ID"]
    sheet_name = os.environ.get("SHEET_NAME", "1")

    result = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A:J"
    ).execute()
    values = result.get("values", [])

    from datetime import datetime as dt
    def parse_date(s):
        for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                return dt.strptime(s, fmt)
            except:
                pass
        return None

    d_inicio = parse_date(data_inicio)
    d_fim = parse_date(data_fim)
    if not d_inicio or not d_fim:
        raise HTTPException(400, "Datas inválidas.")

    rows = []
    for row in values[1:]:
        if len(row) < 5:
            continue
        row_cliente = row[1] if len(row) > 1 else ""
        if row_cliente.strip().lower() != cliente.strip().lower():
            continue
        row_date = parse_date(row[0]) if row else None
        if not row_date:
            continue
        if d_inicio <= row_date <= d_fim:
            rows.append({
                "data": row[0],
                "cliente": row[1] if len(row) > 1 else "",
                "descricao_completa": row[6] if len(row) > 6 else "",
                "valor": row[3] if len(row) > 3 else "",
                "anexo": row[4] if len(row) > 4 else "",
                "responsavel": row[5] if len(row) > 5 else "",
            })

    if not rows:
        raise HTTPException(404, "Nenhum registro encontrado.")

    return {"rows": rows}

@app.get("/relatorio")
def relatorio_page():
    return FileResponse("../frontend/relatorio.html")

@app.post("/gerar-zip")
async def gerar_zip(
    cliente: str = Form(...),
    data_inicio: str = Form(...),
    data_fim: str = Form(...),
):
    try:
        drive, sheets = get_services()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Erro de autenticação: {e}")

    spreadsheet_id = os.environ["SHEETS_ID"]
    sheet_name = os.environ.get("SHEET_NAME", "1")

    # Fetch all rows
    result = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A:J"
    ).execute()
    values = result.get("values", [])

    # Parse date range
    from datetime import datetime as dt
    def parse_date(s):
        for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                return dt.strptime(s, fmt)
            except:
                pass
        return None

    d_inicio = parse_date(data_inicio)
    d_fim = parse_date(data_fim)

    if not d_inicio or not d_fim:
        raise HTTPException(400, "Datas inválidas.")

    # Filter rows by cliente and date range
    matching = []
    for row in values[1:]:
        if len(row) < 5:
            continue
        row_cliente = row[1] if len(row) > 1 else ""
        if row_cliente.strip().lower() != cliente.strip().lower():
            continue
        row_date = parse_date(row[0]) if row else None
        if not row_date:
            continue
        if d_inicio <= row_date <= d_fim:
            matching.append(row)

    if not matching:
        raise HTTPException(404, "Nenhum registro encontrado para esse cliente e período.")

    # Build ZIP with available PDFs
    folder_id = os.environ["DRIVE_FOLDER_COMPROVANTES"]

    # List all files in comprovantes folder
    all_files = drive.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id, name)",
        pageSize=500
    ).execute().get("files", [])

    # Map filename prefix (number) to file
    file_map = {}
    for f in all_files:
        name = f["name"]
        parts = name.split(" ", 1)
        if parts[0].isdigit():
            file_map[parts[0]] = f

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for row in matching:
            anexo = row[4] if len(row) > 4 else "x"
            if anexo and anexo not in ("x", "pendente", ""):
                f = file_map.get(str(int(float(anexo))))
                if f:
                    try:
                        pdf_bytes = download_from_drive(drive, f["id"])
                        zf.writestr(f["name"], pdf_bytes)
                    except Exception as e:
                        logger.warning(f"Erro ao baixar {f['name']}: {e}")

    zip_buf.seek(0)
    zip_bytes = zip_buf.getvalue()

    if not zip_bytes:
        raise HTTPException(404, "Nenhum arquivo de comprovante encontrado para o período.")

    from fastapi.responses import Response
    filename = f"Comprovantes_{cliente.replace(' ','_')}_{data_inicio}_{data_fim}.zip"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.get("/baixar-comprovante/{file_id}")
async def baixar_comprovante(file_id: str):
    try:
        drive, _ = get_services()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Erro de autenticação: {e}")

    try:
        pdf_bytes = download_from_drive(drive, file_id)
        # Get filename
        meta = drive.files().get(fileId=file_id, fields="name").execute()
        filename = meta.get("name", "comprovante.pdf")
    except Exception as e:
        raise HTTPException(500, f"Erro ao baixar arquivo: {e}")

    from fastapi.responses import Response
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@app.get("/health")
def health():
    return {"status": "ok"}
