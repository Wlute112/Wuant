import { useEffect, useRef, useState } from "react";

import { api } from "../../lib/api.js";
import { formatTime } from "../../lib/format.js";
import { useInterval } from "../../hooks/useInterval.js";
import "./job-console.css";

const STATUS_CLASS = {
  running: "is-running",
  completed: "is-positive",
  failed: "is-danger",
  cancelled: "is-dim",
};

export default function JobConsole({ jobs, selectedJobId, onSelectJob, loadStatus = "loading" }) {
  const [logs, setLogs] = useState([]);
  const [logError, setLogError] = useState(null);
  const outputRef = useRef(null);
  const selectedJob = jobs.find((job) => job.id === selectedJobId);

  useEffect(() => {
    if (!selectedJobId) {
      setLogs([]);
      setLogError(null);
      return;
    }
    let cancelled = false;
    setLogError(null);
    api
      .getJobLogs(selectedJobId, 300)
      .then((r) => !cancelled && setLogs(r.lines))
      .catch((error) => !cancelled && setLogError(`Failed to load logs: ${error.message}`));
    return () => {
      cancelled = true;
    };
  }, [selectedJobId]);

  useInterval(() => {
    if (!selectedJobId) return;
    api
      .getJobLogs(selectedJobId, 300)
      .then((r) => {
        setLogs(r.lines);
        setLogError(null);
      })
      .catch((error) => setLogError(`Log stream unavailable: ${error.message}`));
  }, selectedJob?.status === "running" ? 2000 : null);

  useEffect(() => {
    if (selectedJob?.status === "running" && outputRef.current) {
      outputRef.current.scrollTop = outputRef.current.scrollHeight;
    }
  }, [logs, selectedJob?.status]);

  async function cancel(jobId, e) {
    e.stopPropagation();
    try {
      await api.cancelJob(jobId);
    } catch (error) {
      setLogError(`Could not cancel job: ${error.message}`);
    }
  }

  return (
    <section className="job-console" aria-labelledby="jobs-title">
      <div className="job-console__list">
        <h2 id="jobs-title" className="label job-console__heading">
          Jobs
        </h2>
        {loadStatus === "loading" && <div className="job-console__empty">Loading jobs…</div>}
        {loadStatus === "error" && (
          <div className="job-console__empty is-error">Job status unknown — API unavailable</div>
        )}
        {loadStatus === "ready" && jobs.length === 0 && (
          <div className="job-console__empty">No jobs started yet</div>
        )}
        {jobs.map((job) => (
          <div key={job.id} className={`job-console__item ${job.id === selectedJobId ? "is-selected" : ""}`}>
            <button
              type="button"
              className="job-console__select"
              aria-pressed={job.id === selectedJobId}
              onClick={() => onSelectJob(job.id)}
            >
              <span
                aria-hidden="true"
                className={`job-console__status-dot ${STATUS_CLASS[job.status] || ""}`}
              />
              <span className="job-console__item-name">{job.kind}</span>
              <span className="label">{job.status}</span>
              <span className="label job-console__item-time">{formatTime(job.started_at)}</span>
            </button>
            {job.status === "running" && (
              <button
                type="button"
                className="job-console__cancel"
                aria-label={`Cancel ${job.kind} job started ${formatTime(job.started_at)}`}
                onClick={(e) => cancel(job.id, e)}
              >
                cancel
              </button>
            )}
          </div>
        ))}
      </div>
      <div
        ref={outputRef}
        className="job-console__output grain"
        role="log"
        aria-live={selectedJob?.status === "running" ? "polite" : "off"}
        aria-label="Selected job output"
      >
        {logError ? (
          <div className="job-console__empty is-error">{logError}</div>
        ) : logs.length === 0 ? (
          <div className="job-console__empty">
            {selectedJobId ? "No output yet" : "Select a job to view its output"}
          </div>
        ) : (
          <pre className="job-console__pre">{logs.join("\n")}</pre>
        )}
      </div>
    </section>
  );
}
