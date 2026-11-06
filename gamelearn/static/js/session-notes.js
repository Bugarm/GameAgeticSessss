(function () {
  "use strict";
  const form = document.getElementById("session-notes-form");
  if (!form) return;
  const status = document.getElementById("session-notes-status");
  const button = form.querySelector('button[type="submit"]');
  if (!window.Quill) {
    status.textContent = "The notes editor could not load. Refresh to try again.";
    return;
  }
  const BaseImage = Quill.import("formats/image");
  class NoteImage extends BaseImage {
    static formats(node) {
      const formats = super.formats(node);
      if (node.dataset.noteWidth) formats.width = node.dataset.noteWidth;
      if (node.dataset.noteAlign) formats.imageAlign = node.dataset.noteAlign;
      return formats;
    }

    format(name, value) {
      if (name === "width") {
        if (value) {
          this.domNode.dataset.noteWidth = value;
          this.domNode.style.width = `${value}%`;
        } else {
          delete this.domNode.dataset.noteWidth;
          this.domNode.style.removeProperty("width");
        }
        return;
      }
      if (name === "imageAlign") {
        if (value) this.domNode.dataset.noteAlign = value;
        else delete this.domNode.dataset.noteAlign;
        this.domNode.classList.toggle("note-image-center", value === "center");
        this.domNode.classList.toggle("note-image-right", value === "right");
        return;
      }
      super.format(name, value);
    }
  }
  Quill.register(NoteImage, true);
  const BaseVideo = Quill.import("formats/video");
  class YouTubeVideo extends BaseVideo {
    static sanitize(value) {
      return /^https:\/\/www\.youtube-nocookie\.com\/embed\/[A-Za-z0-9_-]{11}$/.test(value) ? value : "about:blank";
    }
    static create(value) {
      const node = super.create(value);
      node.setAttribute("title", "YouTube video player");
      node.setAttribute("loading", "lazy");
      node.setAttribute("referrerpolicy", "strict-origin-when-cross-origin");
      return node;
    }
  }
  Quill.register(YouTubeVideo, true);
  const editor = new Quill("#session-notes-editor", {
    theme: "snow",
    placeholder: "Write your notes about this session…",
    formats: ["bold", "italic", "underline", "strike", "blockquote", "code", "header", "list", "link", "image", "video", "width", "imageAlign"],
    modules: {toolbar: [
      [{header: 1}, {header: 2}],
      ["bold", "italic", "underline", "strike"],
      [{list: "ordered"}, {list: "bullet"}],
      ["blockquote", "code", "link", "image", "video", "clean"]
    ]}
  });
  const imageControls = document.getElementById("note-image-controls");
  const imageSize = document.getElementById("note-image-size");
  const imageSizeValue = document.getElementById("note-image-size-value");
  let selectedImage = null;
  const youtubeControls = document.getElementById("note-youtube-controls");
  const youtubeUrl = document.getElementById("note-youtube-url");
  const youtubeError = document.getElementById("note-youtube-error");
  let youtubeIndex = 0;
  const videoButton = editor.getModule("toolbar").container.querySelector(".ql-video");
  videoButton.setAttribute("title", "YouTube");
  videoButton.setAttribute("aria-label", "Embed YouTube video");
  editor.getModule("toolbar").addHandler("video", () => {
    youtubeIndex = editor.getSelection(true).index;
    selectImage(null);
    youtubeControls.hidden = false;
    youtubeError.textContent = "";
    youtubeUrl.focus();
  });
  function closeYouTubeControls() {
    youtubeControls.hidden = true;
    youtubeUrl.value = "";
    youtubeError.textContent = "";
    editor.focus();
  }
  document.getElementById("cancel-note-youtube").addEventListener("click", closeYouTubeControls);
  function addYouTubeVideo() {
    let id;
    try {
      let source = youtubeUrl.value.trim();
      if (source.startsWith("<")) {
        const parsed = new DOMParser().parseFromString(source, "text/html");
        const frame = parsed.body.firstElementChild;
        if (parsed.body.children.length !== 1 || frame?.tagName !== "IFRAME") throw new Error();
        source = frame.getAttribute("src");
      }
      const url = new URL(source);
      if (!["https:", "http:"].includes(url.protocol) || url.username || url.password || url.port) throw new Error();
      if (url.hostname === "youtu.be") id = url.pathname.slice(1);
      else if (["youtube.com", "www.youtube.com", "m.youtube.com", "www.youtube-nocookie.com", "youtube-nocookie.com"].includes(url.hostname)) {
        if (url.pathname === "/watch" && !url.hostname.includes("nocookie")) id = url.searchParams.get("v");
        else id = url.pathname.match(/^\/(?:embed|shorts|live)\/([A-Za-z0-9_-]{11})\/?$/)?.[1];
      }
      if (!/^[A-Za-z0-9_-]{11}$/.test(id || "")) throw new Error();
    } catch {
      youtubeError.textContent = "Enter a valid YouTube link or YouTube iframe embed code.";
      return;
    }
    const index = Math.min(youtubeIndex, editor.getLength() - 1);
    editor.insertEmbed(index, "video", `https://www.youtube-nocookie.com/embed/${id}`, "user");
    closeYouTubeControls();
    editor.setSelection(index + 1, 0, "user");
  }
  document.getElementById("add-note-youtube").addEventListener("click", addYouTubeVideo);
  youtubeUrl.addEventListener("keydown", event => {
    if (event.key === "Enter") { event.preventDefault(); addYouTubeVideo(); }
    if (event.key === "Escape") { event.preventDefault(); closeYouTubeControls(); }
  });

  function selectImage(image) {
    if (selectedImage) selectedImage.classList.remove("note-image-selected");
    selectedImage = image;
    if (!image) {
      imageControls.classList.add("d-none");
      return;
    }
    image.classList.add("note-image-selected");
    imageSize.value = image.dataset.noteWidth || "100";
    imageSizeValue.value = `${imageSize.value}%`;
    imageControls.classList.remove("d-none");
    imageControls.querySelectorAll("[data-image-align]").forEach(control => {
      control.classList.toggle("active", control.dataset.imageAlign === (image.dataset.noteAlign || "left"));
    });
  }

  function formatSelectedImage(name, value) {
    if (!selectedImage) return;
    const blot = Quill.find(selectedImage);
    if (!blot) return;
    editor.formatText(editor.getIndex(blot), 1, name, value, "user");
  }

  editor.root.addEventListener("click", event => {
    const image = event.target.closest("img");
    selectImage(image && editor.root.contains(image) ? image : null);
  });
  document.addEventListener("pointerdown", event => {
    if (imageControls.contains(event.target) || event.target === selectedImage) return;
    selectImage(null);
  });
  editor.root.addEventListener("keydown", event => {
    if (!["Shift", "Control", "Alt", "Meta"].includes(event.key)) selectImage(null);
  });
  document.addEventListener("focusin", event => {
    if (!editor.root.contains(event.target) && !imageControls.contains(event.target)) selectImage(null);
  });
  imageSize.addEventListener("input", () => {
    imageSizeValue.value = `${imageSize.value}%`;
    formatSelectedImage("width", imageSize.value);
  });
  imageControls.querySelectorAll("[data-image-align]").forEach(control => {
    control.addEventListener("click", () => {
      formatSelectedImage("imageAlign", control.dataset.imageAlign);
      selectImage(selectedImage);
    });
  });
  document.getElementById("remove-note-image").addEventListener("click", () => {
    if (!selectedImage) return;
    const blot = Quill.find(selectedImage);
    if (blot) editor.deleteText(editor.getIndex(blot), 1, "user");
    selectImage(null);
  });
  editor.getModule("toolbar").addHandler("image", () => {
    const picker = document.createElement("input");
    picker.type = "file";
    picker.accept = "image/png,image/jpeg,image/gif,image/webp";
    const range = editor.getSelection(true);
    picker.addEventListener("change", () => {
      const file = picker.files[0];
      if (!file) return;
      if (file.size > 2 * 1024 * 1024) {
        status.textContent = "Each picture must be at most 2 MB.";
        return;
      }
      const reader = new FileReader();
      reader.onerror = () => { status.textContent = "Could not read this picture."; };
      reader.onload = () => {
        editor.insertEmbed(range.index, "image", reader.result, "user");
        editor.setSelection(range.index + 1);
        const blot = editor.getLeaf(range.index)[0];
        if (blot && blot.domNode) selectImage(blot.domNode);
      };
      reader.readAsDataURL(file);
    });
    picker.click();
  });
  editor.root.setAttribute("role", "textbox");
  editor.root.setAttribute("aria-multiline", "true");
  editor.root.setAttribute("aria-labelledby", "my-notes-heading");
  editor.setContents(JSON.parse(document.getElementById("session-notes-data").textContent));
  document.getElementById("notes-modal")?.addEventListener("shown.bs.modal", () => editor.update());
  document.getElementById("notes-modal")?.addEventListener("hidden.bs.modal", () => selectImage(null));
  let saved = JSON.stringify(editor.getContents());
  const dirty = () => JSON.stringify(editor.getContents()) !== saved;
  status.textContent = "";
  button.disabled = false;
  editor.on("text-change", () => {
    if (selectedImage && !editor.root.contains(selectedImage)) selectImage(null);
    status.textContent = dirty() ? "Unsaved notes" : "";
  });
  window.addEventListener("beforeunload", event => {
    if (dirty()) { event.preventDefault(); event.returnValue = ""; }
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const notes = JSON.stringify(editor.getContents());
    if (new Blob([notes]).size > 8 * 1024 * 1024) {
      status.textContent = "Notes including pictures must fit within 8 MB.";
      return;
    }
    button.disabled = true;
    status.textContent = "Saving notes…";
    try {
      const response = await fetch(form.dataset.url, {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-GameLearn-Token": form.dataset.token},
        body: JSON.stringify({notes})
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Notes could not be saved.");
      saved = notes;
      status.textContent = dirty() ? "Saved. You have further unsaved changes." : "Notes saved.";
    } catch (error) {
      status.textContent = error.message || "Notes could not be saved. Please try again.";
    } finally {
      button.disabled = false;
    }
  });
})();
