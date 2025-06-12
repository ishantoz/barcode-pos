import logging
import sqlite3
import threading
import time
import json
import os  # For detecting CPU cores
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS
from escpos.printer import Network

app = Flask(__name__)
CORS(app)  # Allow all origins

DB_PATH = "print_queue.db"
DEFAULT_PRINTER_IP = "192.168.1.100"
DEFAULT_PRINTER_PORT = 9100

MAX_RETRIES = 3
WORKER_COUNT = os.cpu_count() or 1
JOB_RETENTION_DAYS = 7

DEFAULT_BARCODE_WIDTH = 3
DEFAULT_BARCODE_HEIGHT = 100

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(threadName)s: %(message)s",
    handlers=[logging.StreamHandler()]
)

# --- New: Register adapters and converters for datetime ---
def adapt_datetime(dt):
    return dt.isoformat(" ")

def convert_datetime(s):
    return datetime.fromisoformat(s.decode())

sqlite3.register_adapter(datetime, adapt_datetime)
sqlite3.register_converter("timestamp", convert_datetime)
# -------------------------------------------------------

def connect_db():
    return sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)


def init_db():
    with connect_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS print_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT,
                content TEXT NOT NULL,
                status TEXT CHECK(status IN ('pending','processing','done','failed')) DEFAULT 'pending',
                retries INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                meta TEXT
            )
        ''')


def add_to_queue(label: str, content: str, meta: dict):
    with connect_db() as conn:
        conn.execute(
            "INSERT INTO print_jobs (label, content, meta) VALUES (?, ?, ?)",
            (label, content, json.dumps(meta))
        )
    logging.info("Enqueued new job: %s", label)


def get_and_mark_job_processing():
    with connect_db() as conn:
        conn.isolation_level = None
        cur = conn.cursor()
        try:
            cur.execute("BEGIN EXCLUSIVE")
            cur.execute(
                "SELECT id, label, content, meta, retries FROM print_jobs WHERE status='pending' ORDER BY created_at ASC LIMIT 1"
            )
            job = cur.fetchone()
            if not job:
                cur.execute("COMMIT")
                return None
            job_id = job[0]
            cur.execute(
                "UPDATE print_jobs SET status='processing' WHERE id=?", (job_id,)
            )
            cur.execute("COMMIT")
            return job
        except Exception as e:
            logging.error(f"DB error grabbing job: {e}")
            cur.execute("ROLLBACK")
            return None


def mark_job_done(job_id: int):
    with connect_db() as conn:
        conn.execute("UPDATE print_jobs SET status='done' WHERE id=?", (job_id,))
    logging.info(f"Job {job_id} completed.")


def mark_job_failed(job_id: int, retries: int):
    with connect_db() as conn:
        if retries + 1 >= MAX_RETRIES:
            conn.execute(
                "UPDATE print_jobs SET status='failed', retries=? WHERE id=?",
                (retries + 1, job_id)
            )
            logging.error(f"Job {job_id} failed permanently after {retries+1} retries.")
        else:
            conn.execute(
                "UPDATE print_jobs SET status='pending', retries=? WHERE id=?",
                (retries + 1, job_id)
            )
            logging.warning(f"Job {job_id} failed, will retry ({retries+1}/{MAX_RETRIES}).")


def cleanup_jobs():
    while True:
        threshold = datetime.now() - timedelta(days=JOB_RETENTION_DAYS)
        with connect_db() as conn:
            conn.execute(
                "DELETE FROM print_jobs WHERE (status='done' OR status='failed') AND created_at < ?",
                (threshold,)
            )
        time.sleep(24 * 3600)


def print_job(job):
    job_id, label, content, meta_json, retries = job
    try:
        meta = json.loads(meta_json or '{}')
        # how many copies to print
        quantity = int(meta.get('quantity', 1))
        printer_ip = meta.get('printer_ip', DEFAULT_PRINTER_IP)
        printer_port = int(meta.get('printer_port', DEFAULT_PRINTER_PORT))
        printer = Network(printer_ip, printer_port, timeout=10)

        for _ in range(quantity):
            if label:
                printer.set(align='center', width=2, height=2)
                printer.text(label + "\n")
                printer.set(align='center', width=1, height=1)
            printer.set(align='center')
            barcode_width = int(meta.get('barcode_width', DEFAULT_BARCODE_WIDTH))
            barcode_height = int(meta.get('barcode_height', DEFAULT_BARCODE_HEIGHT))
            printer.barcode(
                content,
                'CODE128',
                function_type='A',
                position='below',
                width=barcode_width,
                height=barcode_height
            )
            label_height_mm = float(meta.get('height', 40))
            dots = int(label_height_mm / 0.125)
            printer._raw(bytes([0x1b, 0x4a, dots]))
            printer.cut()

        mark_job_done(job_id)
    except Exception as e:
        logging.error(f"Printer error on job {job_id}: {e}")
        mark_job_failed(job_id, retries)
        time.sleep(5)


def worker_loop():
    while True:
        job = get_and_mark_job_processing()
        if job:
            print_job(job)
        else:
            time.sleep(1)


def start_services():
    for i in range(WORKER_COUNT):
        t = threading.Thread(target=worker_loop, daemon=True, name=f"Worker-{i+1}")
        t.start()
    logging.info(f"Started {WORKER_COUNT} worker threads.")
    ct = threading.Thread(target=cleanup_jobs, daemon=True, name="Cleanup")
    ct.start()
    logging.info("Started daily cleanup thread.")

@app.route('/print/barcodes', methods=['POST'])
def enqueue_print():
    data = request.get_json() or {}
    label = data.get('label', '')
    content = data.get('content')
    meta = data.get('meta', {})
    if not content:
        return jsonify({'error': 'Missing content field.'}), 400
    try:
        add_to_queue(label, content, meta)
        return jsonify({'message': 'Print job queued.'})
    except Exception as e:
        logging.error(f"Enqueue error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/status/print-barcodes', methods=['GET'])
def queue_status():
    with connect_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM print_jobs WHERE status='pending'")
        pending = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM print_jobs WHERE status='processing'")
        processing = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM print_jobs WHERE status='failed'")
        failed = cur.fetchone()[0]
    return jsonify({
        'pending_jobs': pending,
        'processing_jobs': processing,
        'failed_jobs': failed
    })

if __name__ == '__main__':
    init_db()
    start_services()
    app.run(host='0.0.0.0', port=8000, threaded=True)
