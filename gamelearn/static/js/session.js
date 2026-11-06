(function () {
  "use strict";

  function startSessionPage() {
    const timeline = document.getElementById("session-timeline");
    if (!timeline) return;
    const duration = document.getElementById("session-duration");
    const endForm = document.getElementById("end-session-form");
    if (endForm) {
      let ending = false;
      endForm.addEventListener("submit", event => {
        if (ending) {
          event.preventDefault();
          return;
        }
        ending = true;
        const button = endForm.querySelector("button");
        button.disabled = true;
        button.textContent = "Ending session…";
        endForm.setAttribute("aria-busy", "true");
        document.getElementById("end-session-feedback").textContent = "Saving session evidence. This can take a moment for larger changes.";
      });
    }
    let after = Array.from(timeline.querySelectorAll("[data-event-id]"))
      .reduce((maximum, element) => Math.max(maximum, Number(element.dataset.eventId)), 0);

    const updateDuration = () => {
      const started = new Date(duration.dataset.startedAt).getTime();
      const seconds = Math.max(0, Math.floor((Date.now() - started) / 1000));
      const hours = String(Math.floor(seconds / 3600)).padStart(2, "0");
      const minutes = String(Math.floor((seconds % 3600) / 60)).padStart(2, "0");
      const remainder = String(seconds % 60).padStart(2, "0");
      duration.textContent = `${hours}:${minutes}:${remainder}`;
    };

    const poll = async () => {
      try {
        const response = await fetch(`${timeline.dataset.eventsUrl}?after=${after}`, {headers: {Accept: "application/json"}});
        if (!response.ok) return;
        const payload = await response.json();
        document.getElementById("git-status").textContent = payload.git_status;
        payload.events.forEach(event => {
          timeline.appendChild(renderEvent(event));
          after = Math.max(after, event.id);
        });
      } catch (_) {
        document.getElementById("git-status").textContent = "Connection unavailable";
      }
    };

    updateDuration();
    poll();
    window.setInterval(updateDuration, 1000);
    window.setInterval(poll, 3000);
  }

  function renderEvent(event) {
    const article = document.createElement("article");
    article.className = `timeline-event event-${event.source.toLowerCase()} status-${event.status.toLowerCase()}`;
    article.dataset.eventId = event.id;
    const time = document.createElement("time");
    time.textContent = new Date(event.timestamp).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
    const card = document.createElement("div");
    const badge = document.createElement("span");
    badge.className = "badge source-badge";
    badge.textContent = event.source;
    const title = document.createElement("strong");
    title.textContent = ` ${event.title}`;
    card.append(badge, title);
    if (event.description) {
      const description = document.createElement("p");
      description.textContent = event.description;
      card.appendChild(description);
    }
    if (event.changed_files && event.changed_files.length) {
      const list = document.createElement("ul");
      list.className = "timeline-files";
      event.changed_files.forEach(file => {
        const item = document.createElement("li");
        const label = document.createElement("span");
        label.textContent = `${file.change_type} `;
        const path = document.createElement("code");
        path.textContent = file.old_path ? `${file.old_path} → ${file.path}` : file.path;
        item.append(label, path);
        list.appendChild(item);
      });
      card.appendChild(list);
    }
    if (event.metadata) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      const pre = document.createElement("pre");
      summary.textContent = "Evidence details";
      pre.textContent = JSON.stringify(event.metadata, null, 2);
      details.append(summary, pre);
      card.appendChild(details);
    }
    article.append(time, card);
    return article;
  }

  window.GameLearn = {startSessionPage};
})();
