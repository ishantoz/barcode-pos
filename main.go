package main

import (
	"database/sql"
	"errors"
	"fmt"
	"log"
	"net/http"
	"sync"
	"time"

	"barcode-pos/tsplprinter"

	"github.com/labstack/echo/v4"
	"github.com/labstack/echo/v4/middleware"
	_ "github.com/mattn/go-sqlite3"
)

const (
	MaxPrintCount        = 1000
	MaxBarcodeDataLength = 100
	MaxTopTextLength     = 50
	MaxJobAttempts       = 3
	WorkerCount          = 3
	DBPath               = "jobs.db"

	StaleThreshold = 10 * time.Minute
)

const (
	StatusPending    = "pending"
	StatusInProgress = "in_progress"
	StatusFailed     = "failed"
	StatusDone       = "done"
)

type PrintRequest struct {
	VID         string `json:"vid"`
	PID         string `json:"pid"`
	SizeX       int    `json:"sizeX"`
	SizeY       int    `json:"sizeY"`
	Direction   int    `json:"direction"`
	TopText     string `json:"topText"`
	BarcodeData string `json:"barcodeData"`
	PrintCount  int    `json:"printCount"`
}

type Job struct {
	ID        int
	Request   PrintRequest
	Status    string
	Attempts  int
	CreatedAt time.Time
	UpdatedAt time.Time
}

var (
	db   *sql.DB
	dbMu sync.Mutex
)

func main() {
	if err := initDB(); err != nil {
		log.Fatalf("DB init error: %v", err)
	}

	go requeueStaleJobs()

	for i := 0; i < WorkerCount; i++ {
		go worker(i + 1)
	}

	e := echo.New()
	e.HideBanner = true
	e.Use(middleware.Logger())
	e.Use(middleware.Recover())
	e.Use(middleware.CORS())

	fmt.Println("🚀 Barcode Print Service started securely on https://localhost:5000")

	e.GET("/health", func(c echo.Context) error {
		return c.String(http.StatusOK, "OK")
	})

	e.POST("/print-barcode-labels", enqueueHandler)

	e.GET("/job-status/:id", jobStatusHandler)

	certPath := "./certs/cert.pem"
	keyPath := "./certs/cert.key"
	log.Printf("Starting HTTPS server on :5000")
	if err := e.StartTLS(":5000", certPath, keyPath); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("HTTPS server failed: %v", err)
	}
}

func initDB() error {
	var err error
	db, err = sql.Open("sqlite3", DBPath)
	if err != nil {
		return err
	}
	if err := db.Ping(); err != nil {
		return fmt.Errorf("db ping error: %w", err)
	}
	stmt := `CREATE TABLE IF NOT EXISTS jobs (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		vid TEXT, pid TEXT,
		sizeX INTEGER, sizeY INTEGER,
		direction INTEGER, topText TEXT,
		barcodeData TEXT, printCount INTEGER,
		status TEXT, attempts INTEGER,
		createdAt DATETIME, updatedAt DATETIME
	);`
	_, err = db.Exec(stmt)
	return err
}

func requeueStaleJobs() {
	for {
		dbMu.Lock()
		_, err := db.Exec(
			`UPDATE jobs
			 SET status = ?, updatedAt = CURRENT_TIMESTAMP
			 WHERE status = ?
			   AND updatedAt < DATETIME('now', ?)`,
			StatusPending, StatusInProgress, fmt.Sprintf("-%d minutes", int(StaleThreshold.Minutes())),
		)
		dbMu.Unlock()
		if err != nil {
			log.Printf("Error requeuing stale jobs: %v", err)
		}
		time.Sleep(5 * time.Minute)
	}
}

func enqueueHandler(c echo.Context) error {
	var req PrintRequest
	if err := c.Bind(&req); err != nil {
		return c.JSON(http.StatusBadRequest, echo.Map{"error": "Invalid JSON"})
	}

	applyDefaults(&req)
	if err := validateRequest(&req); err != nil {
		return c.JSON(http.StatusBadRequest, echo.Map{"error": err.Error()})
	}

	if err := tsplprinter.CheckPrinterDevice(req.VID, req.PID); err != nil {
		return c.JSON(http.StatusBadRequest, echo.Map{"error": fmt.Sprintf("Printer device not found, please check connected or not: %s", err)})
	}

	now := time.Now()
	dbMu.Lock()
	res, err := db.Exec(
		`INSERT INTO jobs (vid,pid,sizeX,sizeY,direction,topText,barcodeData,printCount,status,attempts,createdAt,updatedAt)
		 VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`,
		req.VID, req.PID, req.SizeX, req.SizeY,
		req.Direction, req.TopText, req.BarcodeData,
		req.PrintCount, StatusPending, 0, now, now,
	)
	dbMu.Unlock()
	if err != nil {
		return c.JSON(http.StatusInternalServerError, echo.Map{"error": "Failed to enqueue job"})
	}
	id, _ := res.LastInsertId()
	return c.JSON(http.StatusAccepted, echo.Map{"jobId": id, "status": StatusPending})
}

func jobStatusHandler(c echo.Context) error {
	id := c.Param("id")
	var status string
	err := db.QueryRow(`SELECT status FROM jobs WHERE id = ?`, id).Scan(&status)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return c.JSON(http.StatusNotFound, echo.Map{"error": "Job not found"})
		}
		return c.JSON(http.StatusInternalServerError, echo.Map{"error": "Error fetching job status"})
	}
	return c.JSON(http.StatusOK, echo.Map{"status": status})
}

func applyDefaults(req *PrintRequest) {
	if req.VID == "" {
		req.VID = "0x0fe6"
	}
	if req.PID == "" {
		req.PID = "0x8800"
	}
	if req.SizeX == 0 {
		req.SizeX = 45
	}
	if req.SizeY == 0 {
		req.SizeY = 35
	}
	if req.PrintCount < 1 {
		req.PrintCount = 1
	} else if req.PrintCount > MaxPrintCount {
		req.PrintCount = MaxPrintCount
	}
	if len(req.TopText) > MaxTopTextLength {
		req.TopText = req.TopText[:MaxTopTextLength]
	}
}

func validateRequest(req *PrintRequest) error {
	if req.BarcodeData == "" {
		return errors.New("barcodeData is required")
	}
	if len(req.BarcodeData) > MaxBarcodeDataLength {
		return fmt.Errorf("barcodeData must not exceed %d chars", MaxBarcodeDataLength)
	}
	return nil
}

func worker(id int) {
	for {
		job, err := fetchJob()
		if err != nil {
			log.Printf("Worker %d: fetch error: %v", id, err)
			time.Sleep(time.Second)
			continue
		}
		if job == nil {
			time.Sleep(time.Second)
			continue
		}
		processJob(id, job)
	}
}

func fetchJob() (*Job, error) {
	dbMu.Lock()
	defer dbMu.Unlock()
	row := db.QueryRow(`
		SELECT id, vid, pid, sizeX, sizeY, direction, topText, barcodeData, printCount, attempts
		FROM jobs WHERE status = ? AND attempts < ? ORDER BY createdAt LIMIT 1`,
		StatusPending, MaxJobAttempts,
	)

	var job Job
	var attempts int
	err := row.Scan(
		&job.ID,
		&job.Request.VID, &job.Request.PID,
		&job.Request.SizeX, &job.Request.SizeY, &job.Request.Direction,
		&job.Request.TopText, &job.Request.BarcodeData,
		&job.Request.PrintCount, &attempts,
	)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, nil
		}
		return nil, err
	}

	_, err = db.Exec(
		`UPDATE jobs SET status = ?, attempts = attempts + 1, updatedAt = CURRENT_TIMESTAMP WHERE id = ?`,
		StatusInProgress, job.ID,
	)
	if err != nil {
		return nil, err
	}

	job.Status = StatusInProgress
	job.Attempts = attempts + 1
	return &job, nil
}

func processJob(workerID int, job *Job) {
	log.Printf("Worker %d processing job %d (attempt %d)", workerID, job.ID, job.Attempts)
	err := tsplprinter.PrintBarcodeLabelTspl(
		job.Request.VID, job.Request.PID,
		job.Request.SizeX, job.Request.SizeY,
		job.Request.Direction, job.Request.TopText,
		job.Request.BarcodeData, job.Request.PrintCount,
	)

	var newStatus string
	if err != nil {
		log.Printf("Worker %d job %d failed: %v", workerID, job.ID, err)
		if job.Attempts >= MaxJobAttempts {
			newStatus = StatusFailed
		} else {
			newStatus = StatusPending
		}
	} else {
		log.Printf("Worker %d job %d done", workerID, job.ID)
		newStatus = StatusDone
	}

	_, uerr := db.Exec(
		`UPDATE jobs SET status = ?, updatedAt = CURRENT_TIMESTAMP WHERE id = ?`,
		newStatus, job.ID,
	)
	if uerr != nil {
		log.Printf("Worker %d update job %d error: %v", workerID, job.ID, uerr)
	}
}
