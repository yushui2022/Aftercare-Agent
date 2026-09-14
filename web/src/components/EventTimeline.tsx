import { formatTime } from "../labels";
import type { CaseEvent } from "../types";

interface Props {
  events: CaseEvent[];
}

export function EventTimeline({ events }: Props) {
  if (events.length === 0) {
    return <p className="empty">暂无事件。</p>;
  }
  return (
    <ol className="timeline">
      {events.map((event) => (
        <li key={event.case_seq}>
          <span className="timeline-seq">#{event.case_seq}</span>
          <div>
            <div className="timeline-type">{event.event_type}</div>
            <div className="timeline-meta">
              {formatTime(event.recorded_at)}
              {event.run_id === null ? "" : ` · ${event.run_id}`}
              {event.reference_id === null ? "" : ` · ${event.reference_id}`}
            </div>
          </div>
        </li>
      ))}
    </ol>
  );
}
