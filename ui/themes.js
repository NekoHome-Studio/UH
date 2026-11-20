(function (root) {
  "use strict";

  function createThemeApplier(style) {
    let applied = [];
    return function apply(theme) {
      for (const name of applied) style.removeProperty(name);
      const variables = Object.entries(theme?.css_variables ?? {});
      if (theme?.font_stack?.length) {
        variables.push(["--uh-font-family", theme.font_stack.map(name => JSON.stringify(name)).join(", ") + ", system-ui, sans-serif"]);
      }
      for (const [name, value] of variables) style.setProperty(name, value);
      applied = variables.map(([name]) => name);
    };
  }

  function mountThemePicker(picker, feedback, getToken) {
    const apply = createThemeApplier(document.documentElement.style);
    let themes = new Map();
    let fonts = [];
    let generation = 0;

    function request(path, options = {}) {
      const token = getToken();
      const headers = { ...options.headers };
      if (token) headers.Authorization = `Bearer ${token}`;
      return fetch(path, { ...options, headers });
    }

    function render(theme, current) {
      apply(theme);
      for (const font of fonts) document.fonts.delete(font);
      fonts = [];
      if (typeof FontFace === "undefined") return;
      for (const resource of theme?.fonts ?? []) {
        request(resource.path)
          .then(response => {
            if (!response.ok) throw new Error("字体加载失败");
            return response.arrayBuffer();
          })
          .then(source => new FontFace(resource.family, source).load())
          .then(font => {
            if (current !== generation) return;
            document.fonts.add(font);
            fonts.push(font);
          })
          .catch(() => {});
      }
    }

    async function reload() {
      const current = ++generation;
      picker.disabled = true;
      feedback.textContent = "";
      render(null, current);
      picker.replaceChildren(new Option("默认主题", ""));
      try {
        const response = await request("/api/v1/themes");
        if (response.status === 401) return;
        if (!response.ok) throw new Error("主题列表加载失败");
        const { data } = await response.json();
        if (current !== generation) return;
        themes = new Map(data.themes.map(theme => [theme.name, theme]));
        for (const name of themes.keys()) picker.add(new Option(name, name));
        picker.value = data.active ?? "";
        render(themes.get(picker.value), current);
        picker.disabled = !themes.size;
      } catch {
        if (current === generation) feedback.textContent = "主题列表加载失败";
      }
    }

    picker.addEventListener("change", async () => {
      const current = ++generation;
      picker.disabled = true;
      feedback.textContent = "";
      try {
        const response = await request("/api/v1/themes/active", {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: picker.value || null }),
        });
        if (!response.ok) throw new Error("主题切换失败");
        const { data } = await response.json();
        if (current === generation) render(themes.get(data.active), current);
      } catch {
        if (current === generation) {
          await reload();
          feedback.textContent = "主题切换失败";
        }
      } finally {
        if (current === generation) picker.disabled = false;
      }
    });
    reload();
    return reload;
  }

  const api = { createThemeApplier, mountThemePicker };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.UHThemes = api;
})(globalThis);
