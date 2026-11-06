(() => {
  const form = document.getElementById('project-import-form');
  const fileInput = document.getElementById('project-backup');
  const preview = document.getElementById('import-preview');
  const projectName = document.getElementById('import-project-name');
  const sessionNames = document.getElementById('import-session-names');
  const feedback = document.getElementById('import-feedback');
  const button = document.getElementById('import-project-button');
  let revision = 0;
  button.disabled = true;
  fileInput.addEventListener('change', async () => {
    const current = ++revision;
    preview.hidden = true;
    projectName.disabled = true;
    sessionNames.replaceChildren();
    form.querySelector('[name="names_reviewed"]')?.remove();
    button.disabled = true;
    feedback.textContent = '';
    const file = fileInput.files[0];
    if (!file) return;
    try {
      if (file.size > 20 * 1024 * 1024) throw new Error('Choose a backup smaller than 20 MB.');
      const backup = JSON.parse(await file.text());
      if (current !== revision) return;
      if (backup?.format !== 'gamelearn-project' || backup.version !== 1 ||
          typeof backup.project?.name !== 'string' || !Array.isArray(backup.sessions) || backup.sessions.length > 10000) {
        throw new Error('Choose a whole-project GameLearn JSON backup.');
      }
      projectName.value = backup.project.name;
      projectName.disabled = false;
      projectName.required = true;
      backup.sessions.forEach((entry, index) => {
        const wrapper = document.createElement('div');
        wrapper.className = 'mb-3';
        const label = document.createElement('label');
        label.className = 'form-label';
        label.htmlFor = `import-session-${index}`;
        label.textContent = `Session ${index + 1} · ${entry.session?.started_at || 'Unknown date'}`;
        const input = document.createElement('input');
        input.className = 'form-control';
        input.id = label.htmlFor;
        input.name = 'session_name';
        input.maxLength = 200;
        input.value = typeof entry.session?.name === 'string' ? entry.session.name : '';
        input.placeholder = 'Default session name';
        wrapper.append(label, input);
        sessionNames.append(wrapper);
      });
      const reviewed = document.createElement('input');
      reviewed.type = 'hidden';
      reviewed.name = 'names_reviewed';
      reviewed.value = '1';
      form.append(reviewed);
      preview.hidden = false;
      button.disabled = false;
      feedback.textContent = `${backup.sessions.length} sessions ready to import. Review the names below.`;
    } catch (error) {
      if (current !== revision) return;
      projectName.disabled = true;
      sessionNames.replaceChildren();
      feedback.textContent = error instanceof SyntaxError ? 'This file is not valid JSON.' : error.message;
    }
  });
})();
