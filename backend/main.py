import os
import io
import httpx
from datetime import datetime
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from typing import List, Optional
import fitz
from PIL import Image

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="../frontend"), name="static")

@app.get("/")
def root():
    return FileResponse("../frontend/index.html")

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

def merge_pdfs(pdf_list: list[bytes]) -> bytes:
    merged = fitz.open()
    for pb in pdf_list:
        src = fitz.open(stream=pb, filetype="pdf")
        merged.insert_pdf(src)
        src.close()
    buf = io.BytesIO()
    merged.save(buf)
    merged.close()
    return buf.getvalue()

# ── Microsoft Graph ──────────────────────────────────────────────────────────

def get_access_token() -> str:
    url = f"https://login.microsoftonline.com/{os.environ['TENANT_ID']}/oauth2/v2.0/token"
    r = httpx.post(url, data={
        "grant_type": "client_credentials",
        "client_id": os.environ["CLIENT_ID"],
        "client_secret": os.environ["CLIENT_SECRET"],
        "scope": "https://graph.microsoft.com/.default",
    })
    r.raise_for_status()
    return r.json()["access_token"]

def gh(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def get_next_anexo_number(token: str) -> int:
    """Scan entire Anexo column (E, index 4), return max + 1."""
    user_id = os.environ["ONEDRIVE_USER_ID"]
    excel_path = os.environ["EXCEL_PATH"]
    sheet_name = os.environ.get("SHEET_NAME", "Planilha1")
    url = (f"https://graph.microsoft.com/v1.0/users/{user_id}/drive/root:/"
           f"{excel_path}:/workbook/worksheets/{sheet_name}/usedRange")
    r = httpx.get(url, headers=gh(token), timeout=30)
    r.raise_for_status()
    max_num = 0
    for row in r.json().get("values", [])[1:]:  # skip header
        if len(row) > 4:
            try:
                n = int(str(row[4]).strip())
                if n > max_num:
                    max_num = n
            except (ValueError, TypeError):
                pass
    return max_num + 1

def upload_to_onedrive(token: str, content: bytes, filename: str, folder: str) -> str:
    user_id = os.environ["ONEDRIVE_USER_ID"]
    path = f"{folder.strip('/')}/{filename}"
    url = f"https://graph.microsoft.com/v1.0/users/{user_id}/drive/root:/{path}:/content"
    r = httpx.put(url, headers={"Authorization": f"Bearer {token}",
                                 "Content-Type": "application/octet-stream"},
                  content=content, timeout=60)
    r.raise_for_status()
    return r.json().get("webUrl", "")

def append_row(token: str, row_data: list):
    user_id = os.environ["ONEDRIVE_USER_ID"]
    excel_path = os.environ["EXCEL_PATH"]
    sheet_name = os.environ.get("SHEET_NAME", "Planilha1")
    base = (f"https://graph.microsoft.com/v1.0/users/{user_id}/drive/root:/"
            f"{excel_path}:/workbook/worksheets/{sheet_name}")

    # Try table first
    r = httpx.post(f"{base}/tables/Tabela2/rows", headers=gh(token),
                   json={"values": [row_data]}, timeout=30)
    if r.status_code in (200, 201):
        return

    # Fallback: next empty row
    rng = httpx.get(f"{base}/usedRange", headers=gh(token), timeout=30)
    rng.raise_for_status()
    next_row = rng.json().get("rowCount", 1) + 1
    col = chr(64 + len(row_data))
    r2 = httpx.patch(f"{base}/range(address='A{next_row}:{col}{next_row}')",
                     headers=gh(token), json={"values": [row_data]}, timeout=30)
    r2.raise_for_status()

# ── Endpoint ─────────────────────────────────────────────────────────────────

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
        token = get_access_token()
    except Exception as e:
        raise HTTPException(500, f"Erro de autenticação Microsoft: {e}")

    # ── Build strings ──
    descricao_completa = descricao
    if processo and processo.strip():
        descricao_completa += f" {processo.strip()}"

    descricao_formula = f"{descricao_completa} - {cliente} - {advogado}"

    data_hoje = datetime.now().strftime("%d/%m/%Y")
    data_file = datetime.now().strftime("%d-%m-%Y")

    def safe(s: str, limit=40) -> str:
        return s.replace("/", "-").replace("\\", "-")[:limit]

    folder = os.environ.get("ONEDRIVE_FOLDER", "Financeiro/Comprovantes")

    # ── Process files ──
    valid_files = [f for f in arquivos if f and f.filename]
    has_file = bool(valid_files)
    anexo_valor = "x"
    next_num = None

    if has_file:
        try:
            next_num = get_next_anexo_number(token)
            anexo_valor = str(next_num)
        except Exception as e:
            raise HTTPException(500, f"Erro ao buscar número de anexo: {e}")

        pdfs = []
        for f in valid_files:
            content = await f.read()
            pdfs.append(to_pdf_bytes(content, f.filename))

        merged = merge_pdfs(pdfs) if len(pdfs) > 1 else pdfs[0]
        filename = f"{next_num} {safe(descricao_completa)} - {safe(cliente)} - {safe(advogado)}.pdf"

        try:
            upload_to_onedrive(token, merged, filename, folder)
        except Exception as e:
            raise HTTPException(500, f"Erro ao salvar no OneDrive: {e}")

    # ── Excel row ──
    # A: Data | B: Cliente | C: Descrição | D: Valor | E: Anexo
    # F: Responsável | G: Descrição completa | H: Reembolsado | I: Obs
    row = [
        data_hoje,
        cliente,
        descricao_formula,
        valor.replace(",", "."),
        anexo_valor,
        advogado,
        descricao_completa,
        "Não",
        obs or "",
    ]

    try:
        append_row(token, row)
    except Exception as e:
        raise HTTPException(500, f"Erro ao gravar no Excel: {e}")

    return {"ok": True, "message": "Pedido registrado com sucesso!"}

@app.get("/health")
def health():
    return {"status": "ok"}
