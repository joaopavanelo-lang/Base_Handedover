import asyncio
from playwright.async_api import async_playwright
from datetime import datetime
import os
import shutil
import gspread
import pandas as pd
from oauth2client.service_account import ServiceAccountCredentials
import time

# --- CONFIGURAÇÃO ---
BASE_DIR = os.getcwd()
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads_shopee")

LISTA_DE_BASES = [
    {
        "nome_log": "Expedidos (Handedover)", 
        "termos_busca": ["Expedidos"], 
        "aba_sheets": "Base Handedover", 
        "prefixo": "PROD"
    }
]

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def rename_file(download_dir, download_path, prefixo):
    try:
        current_hour = datetime.now().strftime("%H")
        new_file_name = f"{prefixo}-{current_hour}.csv"
        new_file_path = os.path.join(download_dir, new_file_name)
        if os.path.exists(new_file_path): os.remove(new_file_path)
        shutil.move(download_path, new_file_path)
        log(f"✅ Arquivo salvo: {new_file_name}")
        return new_file_path
    except Exception as e:
        log(f"❌ Erro ao renomear: {e}")
        return None

def update_google_sheets(csv_file_path, nome_aba):
    try:
        if not os.path.exists("hxh.json"):
            log("⚠️ ERRO: hxh.json não encontrado!")
            return

        scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name("hxh.json", scope)
        client = gspread.authorize(creds)
        sheet = client.open_by_url("https://docs.google.com/spreadsheets/d/1LZ8WUrgN36Hk39f7qDrsRwvvIy1tRXLVbl3-wSQn-Pc/edit#gid=734921183")
        
        try:
            worksheet = sheet.worksheet(nome_aba)
        except:
            log(f"⚠️ A ABA '{nome_aba}' NÃO EXISTE. Baixei o CSV, mas não subi pro Sheets.")
            return

        df = pd.read_csv(csv_file_path).fillna("")
        worksheet.clear()
        worksheet.update([df.columns.values.tolist()] + df.values.tolist())
        log(f"✅ Aba '{nome_aba}' atualizada!")
    except Exception as e:
        log(f"❌ Erro Sheets: {e}")

async def processar_exportacao(page, config):
    nome = config["nome_log"]
    termos = config["termos_busca"]
    aba = config["aba_sheets"]
    prefixo = config["prefixo"]

    log(f"🚀 --- BASE: {nome.upper()} ---")
    
    log("🚚 Acessando Viagens...")
    await page.goto("https://spx.shopee.com.br/#/hubLinehaulTrips/trip")
    await page.wait_for_timeout(8000)

    # 1. LIMPEZA
    await page.evaluate('''() => {
        document.querySelectorAll('.ssc-dialog-wrapper, .ssc-dialog-mask').forEach(el => el.remove());
    }''')
    await page.wait_for_timeout(2000)

    log(f"🔍 Procurando aba '{termos[0]}'...")
    filtro_clicado = False

    # 2. CLIQUE NO FILTRO
    for termo in termos:
        try:
            # Procura pela aba "Expedidos"
            seletor = page.locator(".ant-tabs-tab").filter(has_text=termo).first
            if not await seletor.count():
                seletor = page.get_by_text(termo, exact=True).first

            if await seletor.is_visible():
                await seletor.highlight() 
                log(f"   -> Clicando na aba: '{termo}'")
                await seletor.click(force=True)
                filtro_clicado = True
                break
        except: continue

    if not filtro_clicado:
        log(f"⚠️ ALERTA: Não achei a aba {nome}.")
        return
    
    log("⏳ Aguardando tabela atualizar (5s)...")
    await page.wait_for_timeout(5000)

    # 3. EXPORTAR (Via JS para garantir)
    log("📤 Exportando...")
    try:
        btn_export = page.get_by_role("button", name="Exportar").first
        await btn_export.highlight()
        # MUDANÇA: Usando evaluate aqui também
        await btn_export.evaluate("element => element.click()")
    except:
        log("⚠️ Falha Exportar.")
        return

    await page.wait_for_timeout(5000)

    log("📂 Centro de Tarefas...")
    await page.goto("https://spx.shopee.com.br/#/taskCenter/exportTaskCenter")
    
    try:
        await page.wait_for_selector("text=Exportar tarefa", timeout=10000)
        await page.get_by_text("Exportar tarefa").or_(page.get_by_text("Export Task")).click(force=True)
    except: pass

    log(f"⬇️ Aguardando botão 'Baixar'...")
    download_sucesso = False
    
    for i in range(1, 10):
        try:
            await page.wait_for_selector("text=Baixar", timeout=60000)
            
            log(f"⚡ Baixando (Tentativa {i})...")
            async with page.expect_download(timeout=60000) as download_info:
                btn_baixar = page.locator("text=Baixar").first
                await btn_baixar.highlight()
                
                # --- CORREÇÃO PRINCIPAL AQUI ---
                # Troquei .click(force=True) por .evaluate()
                # Isso força o clique via JavaScript, impossível de errar.
                await btn_baixar.evaluate("element => element.click()")
                # -------------------------------
            
            download = await download_info.value
            path = os.path.join(DOWNLOAD_DIR, download.suggested_filename)
            await download.save_as(path)
            
            final_path = rename_file(DOWNLOAD_DIR, path, prefixo)
            if final_path:
                update_google_sheets(final_path, aba)
            
            download_sucesso = True
            break
        
        except Exception:
            log(f"⏳ Falha no download. Recarregando página...")
            try:
                await page.reload()
                await page.wait_for_load_state("networkidle")
                await page.get_by_text("Exportar tarefa").or_(page.get_by_text("Export Task")).click(force=True)
            except: break
    
    if not download_sucesso:
        log(f"❌ Timeout base {nome}.")

async def main():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    
    async with async_playwright() as p:
        log("🚀 Abrindo navegador...")
        browser = await p.chromium.launch(
            headless=False, 
            slow_mo=50,
            args=["--disable-infobars", "--disable-translate", "--disable-notifications", "--no-first-run"]
        )
        
        context = await browser.new_context(
            accept_downloads=True, 
            viewport={'width': 1366, 'height': 768},
            locale='pt-BR',
            timezone_id='America/Sao_Paulo',
            permissions=['geolocation'],
            geolocation={'latitude': -23.5505, 'longitude': -46.6333}
        )
        
        page = await context.new_page()

        try:
            log("🔐 Acessando SPX...")
            await page.goto("https://spx.shopee.com.br/")
            await page.wait_for_selector('xpath=//*[@placeholder="Ops ID"]', timeout=10000)
            
            await page.locator('xpath=//*[@placeholder="Ops ID"]').fill('Ops134294')
            await page.locator('xpath=//*[@placeholder="Senha"]').fill('@Shopee123')
            
            log("⏳ Esperando 1s...")
            await page.wait_for_timeout(1000) 

            log("👆 Entrar...")
            await page.locator('xpath=/html/body/div[1]/div/div[2]/div/div/div[1]/div[3]/form/div/div/button').click()
            
            try:
                await page.wait_for_url("**/#/**", timeout=60000) 
                log("✅ Logado!")
            except:
                log("⚠️ Login demorou, mas seguindo...")

            await page.wait_for_load_state("networkidle")

            # Limpeza inicial
            log("🧹 Limpando...")
            await page.wait_for_timeout(3000)
            try: await page.keyboard.press("Escape")
            except: pass
            
            await page.evaluate('''() => {
                document.querySelectorAll('.ssc-dialog-wrapper, .ssc-dialog-mask').forEach(el => el.remove());
            }''')

            # EXECUTA A ÚNICA BASE DA LISTA
            for config in LISTA_DE_BASES:
                await processar_exportacao(page, config)

            log("🎉 FINALIZADO COM SUCESSO!")
            await browser.close()

        except Exception as e:
            log(f"❌ Erro Fatal: {e}")
            await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
