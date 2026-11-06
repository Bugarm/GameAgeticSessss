(() => {
  const root = document.querySelector("#learning-chat");
  if (!root) return;

  const picker = document.querySelector("#learning-chat-picker");
  const messages = document.querySelector("#learning-chat-messages");
  const form = document.querySelector("#learning-chat-form");
  const question = document.querySelector("#learning-chat-question");
  const send = document.querySelector("#learning-chat-send");
  const status = document.querySelector("#learning-chat-status");
  const newChat = document.querySelector("#new-learning-chat");
  const learningFrame = document.querySelector("#learning-page-frame");
  const fileToast = document.querySelector("#learning-file-toast");
  let conversations = [];
  let activeConversationId = null;
  let pollTimer = null;
  let toastTimer = null;

  const formatGenerationStatus = (generation) => {
    if (!generation) return "Codex is answering…";
    const usage = generation.token_usage;
    const tokenText = usage
      ? `${Number(usage.inputTokens || 0).toLocaleString()} input tokens, ${Number(usage.cachedInputTokens || 0).toLocaleString()} cached`
      : `about ${Number(generation.estimated_input_tokens || 0).toLocaleString()} estimated input tokens`;
    const activityText = generation.activity_count
      ? `${generation.activity_count} CLI updates`
      : "waiting for CLI activity";
    return `${generation.message} ${activityText}; ${tokenText}.`;
  };

  const setBusy = (busy, text = "") => {
    send.disabled = busy;
    question.disabled = busy;
    picker.disabled = busy;
    newChat.disabled = busy;
    status.textContent = text;
  };

  const showFileToast = (text, isError = false) => {
    if (!fileToast) return;
    window.clearTimeout(toastTimer);
    fileToast.textContent = text;
    fileToast.classList.toggle("is-error", isError);
    fileToast.hidden = false;
    toastTimer = window.setTimeout(() => { fileToast.hidden = true; }, 3200);
  };

  const notifyLearningFrame = (type, submissionId, error = "") => {
    if (!learningFrame?.contentWindow || !submissionId) return;
    learningFrame.contentWindow.postMessage({ type, submissionId, error }, "*");
  };

  const appendInlineText = (container, text) => {
    const inlineCode = /`([^`]+)`|\*\*([^*]+)\*\*/g;
    let cursor = 0;
    for (const match of text.matchAll(inlineCode)) {
      container.append(document.createTextNode(text.slice(cursor, match.index)));
      const element = document.createElement(match[1] !== undefined ? "code" : "strong");
      element.textContent = match[1] !== undefined ? match[1] : match[2];
      container.append(element);
      cursor = match.index + match[0].length;
    }
    container.append(document.createTextNode(text.slice(cursor)));
  };

  const highlightChatCode = (code) => {
    const source = code.textContent;
    const pattern = /(\/\*[\s\S]*?\*\/|\/\/[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\b(?:class|struct|interface|enum|namespace|using|public|private|protected|internal|static|readonly|const|void|return|if|else|for|foreach|while|switch|case|break|continue|new|try|catch|finally|throw|true|false|null|async|await|var|this|base|override|virtual|sealed|ref|out|in)\b|\b(?:bool|byte|char|decimal|double|float|int|long|object|short|string|GameObject|Transform|Vector2|Vector3|Quaternion|MonoBehaviour)\b|\b\d+(?:\.\d+)?[fFdDmM]?\b)/g;
    const fragment = document.createDocumentFragment();
    let cursor = 0;
    for (const match of source.matchAll(pattern)) {
      fragment.append(document.createTextNode(source.slice(cursor, match.index)));
      const token = document.createElement("span");
      const value = match[0];
      if (value.startsWith("//") || value.startsWith("/*")) token.className = "chat-syntax-comment";
      else if (value.startsWith('"') || value.startsWith("'")) token.className = "chat-syntax-string";
      else if (/^\d/.test(value)) token.className = "chat-syntax-number";
      else if (/^(bool|byte|char|decimal|double|float|int|long|object|short|string|GameObject|Transform|Vector2|Vector3|Quaternion|MonoBehaviour)$/.test(value)) token.className = "chat-syntax-type";
      else token.className = "chat-syntax-keyword";
      token.textContent = value;
      fragment.append(token);
      cursor = match.index + value.length;
    }
    fragment.append(document.createTextNode(source.slice(cursor)));
    code.replaceChildren(fragment);
  };

  const renderTutorMarkdown = (container, content) => {
    const fence = /```([\w#+.-]*)\s*\r?\n([\s\S]*?)```/g;
    let cursor = 0;
    const appendText = (text) => {
      const blocks = text.trim().split(/\n\s*\n/).filter(Boolean);
      blocks.forEach((block) => {
        const lines = block.split(/\r?\n/);
        if (lines.every((line) => /^\s*[-*]\s+/.test(line))) {
          const list = document.createElement("ul");
          lines.forEach((line) => {
            const item = document.createElement("li");
            appendInlineText(item, line.replace(/^\s*[-*]\s+/, ""));
            list.append(item);
          });
          container.append(list);
        } else {
          const paragraph = document.createElement("p");
          appendInlineText(paragraph, lines.join("\n"));
          container.append(paragraph);
        }
      });
    };
    for (const match of content.matchAll(fence)) {
      appendText(content.slice(cursor, match.index));
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      if (match[1]) code.dataset.language = match[1];
      code.textContent = match[2].replace(/\s+$/, "");
      highlightChatCode(code);
      pre.append(code);
      container.append(pre);
      cursor = match.index + match[0].length;
    }
    appendText(content.slice(cursor));
  };

  const renderMessages = () => {
    const active = conversations.find((item) => item.id === activeConversationId);
    messages.replaceChildren();
    if (!active || !active.messages.length) {
      const empty = document.createElement("p");
      empty.className = "text-secondary small mb-0";
      empty.textContent = "Ask a question about the changes, code, or ideas in this learning session.";
      messages.append(empty);
      return;
    }
    active.messages.forEach((item) => {
      const bubble = document.createElement("div");
      bubble.className = `learning-chat-message ${item.role}`;
      if (item.status === "PENDING") {
        bubble.classList.add("pending");
        bubble.textContent = "Codex is thinking…";
      } else {
        if (item.status === "FAILED") bubble.classList.add("failed");
        if (item.role === "tutor" && item.status !== "FAILED") renderTutorMarkdown(bubble, item.content);
        else bubble.textContent = item.content;
      }
      messages.append(bubble);
    });
    messages.scrollTop = messages.scrollHeight;
  };

  const renderPicker = () => {
    picker.replaceChildren();
    const fresh = document.createElement("option");
    fresh.value = "";
    fresh.textContent = "New chat";
    picker.append(fresh);
    conversations.forEach((item) => {
      const option = document.createElement("option");
      option.value = String(item.id);
      option.textContent = item.title;
      picker.append(option);
    });
    picker.value = activeConversationId ? String(activeConversationId) : "";
  };

  const loadChats = async (preferredId = null) => {
    const response = await fetch(root.dataset.chatsUrl, { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error("GameLearn could not load saved chats.");
    conversations = (await response.json()).conversations;
    if (preferredId && conversations.some((item) => item.id === preferredId)) {
      activeConversationId = preferredId;
    } else if (activeConversationId && !conversations.some((item) => item.id === activeConversationId)) {
      activeConversationId = null;
    }
    renderPicker();
    renderMessages();
  };

  const pollAnswer = (messageId, conversationId) => {
    window.clearTimeout(pollTimer);
    const poll = async () => {
      try {
        const url = root.dataset.statusUrlTemplate.replace(/\/0$/, `/${messageId}`);
        const response = await fetch(url, { headers: { Accept: "application/json" } });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "GameLearn could not load the answer.");
        await loadChats(conversationId);
        if (payload.status === "PENDING") {
          setBusy(true, formatGenerationStatus(payload.generation));
          pollTimer = window.setTimeout(poll, 1000);
          return;
        }
        setBusy(false, payload.status === "FAILED" ? "Codex could not answer." : "Answer saved.");
        question.focus();
      } catch (error) {
        setBusy(false, error.message);
      }
    };
    pollTimer = window.setTimeout(poll, 700);
  };

  const submitQuestion = async (promptText, conversationId, options = {}) => {
    const worksheet = options.worksheet || null;
    const text = worksheet
      ? `${worksheet.question}\n\nStudent answer: ${worksheet.student_answer}`.trim()
      : String(promptText || "").trim();
    if (!text) return;
    if (!worksheet && text.length > 4000) {
      status.textContent = "This question is too long to send.";
      return;
    }
    setBusy(true, "Sending question…");
    try {
      const response = await fetch(root.dataset.askUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-GameLearn-Token": root.dataset.csrfToken },
        body: JSON.stringify(worksheet
          ? { worksheet, conversation_id: null }
          : { question: text, conversation_id: conversationId }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "GameLearn could not send the question.");
      activeConversationId = payload.conversation.id;
      question.value = "";
      await loadChats(activeConversationId);
      if (worksheet) notifyLearningFrame("gamelearn:tutor-chat-opened", options.submissionId);
      setBusy(true, "Codex is answering…");
      pollAnswer(payload.pending_message_id, activeConversationId);
    } catch (error) {
      setBusy(false, error.message);
      if (worksheet) notifyLearningFrame("gamelearn:tutor-chat-failed", options.submissionId, error.message);
    }
  };

  const openRecordedFile = async (path) => {
    showFileToast(`Opening ${path}…`);
    try {
      const response = await fetch(root.dataset.openFileUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-GameLearn-Token": root.dataset.csrfToken },
        body: JSON.stringify({ path }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "GameLearn could not open that file.");
      showFileToast(`${payload.filename} opened in your configured editor.`);
    } catch (error) {
      showFileToast(error.message, true);
    }
  };

  picker.addEventListener("change", () => {
    activeConversationId = picker.value ? Number(picker.value) : null;
    status.textContent = "";
    renderMessages();
  });
  newChat.addEventListener("click", () => {
    activeConversationId = null;
    picker.value = "";
    status.textContent = "";
    renderMessages();
    question.focus();
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = question.value.trim();
    if (!text) return;
    await submitQuestion(text, activeConversationId);
  });

  window.addEventListener("message", async (event) => {
    if (!learningFrame || event.source !== learningFrame.contentWindow) return;
    const message = event.data;
    if (!message || typeof message !== "object") return;
    if (message.type === "gamelearn:tutor-question" && typeof message.question === "string") {
      activeConversationId = null;
      renderPicker();
      renderMessages();
      await submitQuestion(message.question, null);
    } else if (
      message.type === "gamelearn:worksheet-submission"
      && typeof message.worksheetQuestion === "string"
      && typeof message.studentAnswer === "string"
      && typeof message.submissionId === "string"
    ) {
      activeConversationId = null;
      renderPicker();
      renderMessages();
      await submitQuestion("", null, {
        submissionId: message.submissionId,
        worksheet: {
          question: message.worksheetQuestion,
          student_answer: message.studentAnswer,
        },
      });
    } else if (message.type === "gamelearn:open-file" && typeof message.path === "string") {
      await openRecordedFile(message.path);
    }
  });

  loadChats().catch((error) => { status.textContent = error.message; });
})();
