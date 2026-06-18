import os
import io
import json
import logging
from datetime import datetime
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from typing import List, Optional
import fitz
from PIL import Image
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="../frontend"), name="static")

@app.get("/")
def root():
    return FileResponse("../frontend/index.html")

@app.get("/comprovante")
def comprovante_page():
    return FileResponse("../frontend/comprovante.html")

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

def get_services():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    drive = build("drive", "v3", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)
    return drive, sheets

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
        fields="files(id, name)",
        orderBy="name"
    ).execute()
    return results.get("files", [])

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
        data_hoje,
        cliente,
        descricao_formula,
        valor.replace(",", "."),
        "pendente" if has_file else "x",
        advogado,
        descricao_completa,
        "Não",
        obs or "",
        file_drive_id,
    ]

    try:
        append_row_sheets(sheets, row)
    except Exception as e:
        raise HTTPException(500, f"Erro ao gravar no Sheets: {e}")

    return {"ok": True, "message": "Pedido registrado com sucesso!"}


@app.get("/pendentes")
async def get_pendentes():
    try:
        drive, _ = get_services()
        files = list_pending(drive)
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
        upload_to_drive(drive, merged, final_name, folder_comprovantes)
        delete_from_drive(drive, file_id)
        update_anexo_in_sheets(sheets, file_id, next_num)
    except Exception as e:
        logger.error(f"Erro ao anexar comprovante: {e}")
        raise HTTPException(500, f"Erro: {e}")

    return {"ok": True, "numero": next_num, "filename": final_name}


@app.get("/health")
def health():
    return {"status": "ok"}
