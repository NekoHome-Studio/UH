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
    let requestId = 0;
    let pending = false;
    let catalog = "";
    let active = "";

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

    async function refresh(reset = false) {
      if (pending && !reset) return;
      const current = ++requestId;
      pending = true;
      if (reset) {
        catalog = active = "";
        themes.clear();
        picker.disabled = true;
        feedback.textContent = "";
        picker.replaceChildren(new Option("默认主题", ""));
        render(null, ++generation);
      }
      try {
        const response = await request("/api/v1/themes", { cache: "no-store" });
        if (response.status === 401) return;
        if (!response.ok) throw new Error("主题列表加载失败");
        const { data } = await response.json();
        if (current !== requestId) return;
        const nextCatalog = JSON.stringify(data.themes);
        const nextActive = data.active ?? "";
        if (nextCatalog !== catalog || nextActive !== active) {
          if (nextCatalog !== catalog) {
            catalog = nextCatalog;
            themes = new Map(data.themes.map(theme => [theme.name, theme]));
            picker.replaceChildren(new Option("默认主题", ""));
            for (const name of themes.keys()) picker.add(new Option(name, name));
          }
          active = picker.value = nextActive;
          render(themes.get(active), ++generation);
        }
        if (feedback.textContent === "主题列表加载失败") feedback.textContent = "";
      } catch {
        if (current === requestId) feedback.textContent = "主题列表加载失败";
      } finally {
        if (current === requestId) {
          pending = false;
          picker.disabled = !themes.size;
        }
      }
    }

    picker.addEventListener("change", async () => {
      const current = ++requestId;
      pending = true;
      picker.disabled = true;
      feedback.textContent = "";
      try {
        const response = await request("/api/v1/themes/active", {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: picker.value || null }),
        });
        if (!response.ok) throw new Error("主题切换失败");
        const { data } = await response.json();
        if (current === requestId) {
          active = picker.value = data.active ?? "";
          render(themes.get(active), ++generation);
        }
      } catch {
        if (current === requestId) {
          picker.value = active;
          feedback.textContent = "主题切换失败";
        }
      } finally {
        if (current === requestId) {
          pending = false;
          picker.disabled = !themes.size;
        }
      }
    });
    function refreshVisible() {
      if (!document.hidden) return refresh();
    }
    window.addEventListener("focus", refreshVisible);
    document.addEventListener("visibilitychange", refreshVisible);
    setInterval(refreshVisible, 3000);
    refresh(true);
    return () => refresh(true);
  }

  const api = { createThemeApplier, mountThemePicker };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.UHThemes = api;
})(globalThis);
