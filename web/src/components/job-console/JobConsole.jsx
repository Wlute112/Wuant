import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../../lib/api.js";
import { formatTime } from "../../lib/format.js";
import {
  isJobActive,
  jobKindLabel,
  jobStatusLabel,
  orderedJobRows,
} from "../../lib/jobs.js";
import { useInterval } from "../../hooks/useInterval.js";
import "./job-console.css";

const STATUS_CLASS = {
  starting: "is-starting",
  running: "is-running",
  cancelling: "is-cancelling",
  completed: "is-positive",
  failed: "is-danger",
  cancelled: "is-dim",
};

function emptyOutputCopy(job) {
  if (!job) return "Select a job to view its output";
  if (job.status === "starting") return "Process accepted. Waiting for output.";
  if (job.status === "cancelling") return "Cancellation requested. Waiting for clean shutdown.";
  if (job.status === "completed") return "Job completed without console output.";
  if (job.status === "cancelled") return "Job cancelled without console output.";
  if (job.status === "failed") return "Job failed before console output was written.";
  return "No output yet";
}

export default function JobConsole({
  jobs,
  selectedJobId,
  onSelectJob,
  onJobUpdated,
  loadStatus = "loading",
}) {
  const [logs, setLogs] = useState([]);
  const [logError, setLogError] = useState(null);
  const [pendingCancelId, setPendingCancelId] = useState(null);
  const outputRef = useRef(null);
  const selectedJob = jobs.find((job) => job.id === selectedJobId);
  const selectedActive = isJobActive(selectedJob);
  const rows = orderedJobRows(jobs);

  const loadLogs = useCallback(async () => {
    if (!selectedJobId) return;
    try {
      const result = await api.getJobLogs(selectedJobId, 300);
      setLogs(result.lines);
      setLogError(null);
    } catch (error) {
      setLogError(`Log stream unavailable: ${error.message}`);
    }
  }, [selectedJobId]);

  useEffect(() => {
    if (!selectedJobId) {
      setLogs([]);
      setLogError(null);
      return undefined;
    }
    let disposed = false;
    setLogError(null);
    api
      .getJobLogs(selectedJobId, 300)
      .then((result) => !disposed && setLogs(result.lines))
      .catch((error) => !disposed && setLogError(`Failed to load logs: ${error.message}`));
    return () => {
      disposed = true;
    };
  }, [selectedJobId]);

  useInterval(loadLogs, selectedActive ? 2000 : null);

  useEffect(() => {
    if (selectedActive && outputRef.current) {
      outputRef.current.scrollTop = outputRef.current.scrollHeight;
    }
  }, [logs, selectedActive]);

  async function cancel(job, event) {
    event.stopPropagation();
    setPendingCancelId(job.id);
    setLogError(null);
    onJobUpdated?.({
      ...job,
      status: "cancelling",
      cancel_requested_at: new Date().toISOString(),
    });
    try {
      const updated = await api.cancelJob(job.id);
      onJobUpdated?.(updated);
    } catch (error) {
      onJobUpdated?.(job);
      setLogError(`Could not cancel job: ${error.message}`);
    } finally {
      setPendingCancelId(null);
    }
  }

  const relatedJobs = selectedJob
    ? jobs.filter((job) => (
        job.parent_job_id === selectedJob.id
        || job.id === selectedJob.parent_job_id
      ))
    : [];

  return (
    <section className="job-console" aria-labelledby="jobs-title">
      <div className="job-console__list">
        <h2 id="jobs-title" className="label job-console__heading">
          Durable jobs
        </h2>
        {loadStatus === "loading" && <div className="job-console__empty">Loading jobs...</div>}
        {loadStatus === "error" && (
          <div className="job-console__empty is-error">Job registry unknown - API unavailable</div>
        )}
        {loadStatus === "ready" && jobs.length === 0 && (
          <div className="job-console__empty">No jobs started yet</div>
        )}
        {rows.map(({ job, depth }) => {
          const stopping = job.status === "cancelling" || pendingCancelId === job.id;
          const canCancel = depth === 0 && isJobActive(job) && !stopping;
          return (
            <div
              key={job.id}
              className={`job-console__item ${depth ? "is-companion" : ""} ${job.id === selectedJobId ? "is-selected" : ""}`}
            >
              <button
                type="button"
                className="job-console__select"
                aria-pressed={job.id === selectedJobId}
                aria-label={`${jobKindLabel(job.kind)}, ${jobStatusLabel(job.status)}, started ${formatTime(job.started_at)}`}
                onClick={() => onSelectJob(job.id)}
              >
                <span
                  aria-hidden="true"
                  className={`job-console__status-dot ${STATUS_CLASS[job.status] || ""}`}
                />
                <span className="job-console__item-copy">
                  <span className="job-console__item-name">{jobKindLabel(job.kind)}</span>
                  <span className="job-console__item-meta label">
                    {depth > 0 && <span>Linked</span>}
                    <span>{jobStatusLabel(job.status)}</span>
                  </span>
                </span>
              </button>
              {(canCancel || stopping) && (
                <button
                  type="button"
                  className="job-console__cancel"
                  aria-label={`Cancel ${jobKindLabel(job.kind)} started ${formatTime(job.started_at)}`}
                  aria-busy={stopping}
                  disabled={stopping}
                  onClick={(event) => cancel(job, event)}
                >
                  {stopping ? "Stopping" : job.kind === "paper" || job.kind === "live" ? "Stop" : "Cancel"}
                </button>
              )}
            </div>
          );
        })}
      </div>
      <div className="job-console__output-shell">
        {selectedJob && (
          <header className="job-console__output-header">
            <div className="job-console__output-title">
              <span className="label">{jobKindLabel(selectedJob.kind)}</span>
              <strong className="num">{jobStatusLabel(selectedJob.status)}</strong>
              <span className="job-console__job-id num">{selectedJob.id}</span>
              <span className="job-console__job-time label">{formatTime(selectedJob.started_at)}</span>
            </div>
            {relatedJobs.length > 0 && (
              <div className="job-console__relations" aria-label="Linked jobs">
                {relatedJobs.map((job) => (
                  <button key={job.id} type="button" onClick={() => onSelectJob(job.id)}>
                    {job.parent_job_id ? "Open supervisor" : "Open session"}
                  </button>
                ))}
              </div>
            )}
          </header>
        )}
        {selectedJob?.failure_reason && (
          <div className="job-console__failure" role="alert">
            <span className="label">Failure</span>
            <span>{selectedJob.failure_reason}</span>
          </div>
        )}
        <div
          ref={outputRef}
          className="job-console__output grain"
          role="log"
          aria-live={selectedActive ? "polite" : "off"}
          aria-label="Selected job output"
        >
          {logError ? (
            <div className="job-console__empty is-error job-console__retry">
              <span>{logError}</span>
              <button type="button" onClick={loadLogs}>Retry logs</button>
            </div>
          ) : logs.length === 0 ? (
            <div className="job-console__empty">{emptyOutputCopy(selectedJob)}</div>
          ) : (
            <pre className="job-console__pre">{logs.join("\n")}</pre>
          )}
        </div>
      </div>
    </section>
  );
}
