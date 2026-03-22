from __future__ import annotations

import json
from typing import Any, Dict, List

from .base import BaseContextProvider, ProviderError
from .models import ContextCandidate, ContextFetchRequest


def _format_timestamp(raw_seconds: Any) -> str:
    try:
        total_seconds = int(float(raw_seconds))
    except (TypeError, ValueError):
        return ""
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


class YouGlishProvider(BaseContextProvider):
    name = "youglish_widget"
    display_name = "YouGlish"

    def fetch_candidates(self, request: ContextFetchRequest) -> List[ContextCandidate]:
        try:
            from aqt.qt import (
                QEventLoop,
                QObject,
                QTimer,
                QUrl,
                pyqtSlot,
            )
            from PyQt6.QtWebChannel import QWebChannel
            from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
        except Exception as exc:
            raise ProviderError(f"Qt web engine is unavailable: {exc}") from exc

        collected: List[ContextCandidate] = []
        errors: List[str] = []

        class Bridge(QObject):
            @pyqtSlot(str)
            def addCandidate(self, payload: str) -> None:  # noqa: N802 - Qt slot name
                try:
                    data = json.loads(payload)
                except Exception:
                    return
                raw_payload = dict(data.get("raw_payload") or {})
                candidate = ContextCandidate(
                    sentence_text=str(data.get("sentence_text", "")).strip(),
                    matched_term=str(data.get("matched_term", "") or ""),
                    source_title=str(data.get("source_title", "") or ""),
                    source_url=str(data.get("source_url", "") or ""),
                    timestamp=_format_timestamp(data.get("timestamp")),
                    video_id=str(data.get("video_id", "") or ""),
                    provider_name=YouGlishProvider.display_name,
                    raw_payload=raw_payload,
                )
                if candidate.sentence_text:
                    collected.append(candidate)

            @pyqtSlot(str)
            def finish(self, _payload: str) -> None:  # noqa: N802 - Qt slot name
                loop.quit()

            @pyqtSlot(str)
            def fail(self, payload: str) -> None:  # noqa: N802 - Qt slot name
                errors.append(payload)
                loop.quit()

        profile = QWebEngineProfile()
        page = QWebEnginePage(profile)
        channel = QWebChannel(page)
        bridge = Bridge()
        channel.registerObject("bridge", bridge)
        page.setWebChannel(channel)
        page.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True
        )
        page.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True
        )
        loop = QEventLoop()
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)

        params_json = json.dumps(
            {"query": request.query, "maxCandidates": request.max_candidates}
        )
        html = f"""
<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <script src="qrc:///qtwebchannel/qwebchannel.js"></script>
    <script src="https://youglish.com/public/emb/widget.js"></script>
  </head>
  <body>
    <div id="widget-root"></div>
    <script>
      const params = {params_json};
      let widget = null;
      let bridge = null;
      let currentVideo = "";
      let seen = new Set();
      let count = 0;

      function cleanCaption(value) {{
        let text = String(value || "");
        try {{
          if (/%u[0-9A-Fa-f]{{4}}/.test(text) || /%[0-9A-Fa-f]{{2}}/.test(text)) {{
            text = unescape(text);
          }}
        }} catch (error) {{
        }}
        return text.replace(/\\[\\[\\[/g, "").replace(/\\]\\]\\]/g, "").trim();
      }}

      function buildUrl(payload) {{
        if (payload && payload.cid) {{
          return "https://youglish.com/getbyid/" + payload.cid + "/" + encodeURIComponent(params.query) + "/korean/all";
        }}
        if (payload && payload.id) {{
          return "https://youglish.com/getbyid/" + payload.id + "/" + encodeURIComponent(params.query) + "/korean/all";
        }}
        if (currentVideo) {{
          return "https://www.youtube.com/watch?v=" + currentVideo;
        }}
        return "";
      }}

      function maybeComplete() {{
        if (count >= params.maxCandidates && bridge) {{
          bridge.finish(JSON.stringify({{status: "ok"}}));
        }}
      }}

      function queueNext() {{
        if (!widget || count >= params.maxCandidates) {{
          maybeComplete();
          return;
        }}
        setTimeout(function() {{
          try {{
            widget.next();
          }} catch (error) {{
            if (bridge) {{
              bridge.finish(JSON.stringify({{status: "partial"}}));
            }}
          }}
        }}, 150);
      }}

      function onFetchDone(event) {{
        if (!event || Number(event.totalResult || 0) === 0) {{
          bridge.finish(JSON.stringify({{status: "empty"}}));
        }}
      }}

      function onVideoChange(event) {{
        currentVideo = event && event.video ? event.video : "";
      }}

      function onCaptionChange(event) {{
        const sentence = cleanCaption(event && event.caption);
        if (!sentence) {{
          return;
        }}
        const uniqueKey = currentVideo + "|" + sentence;
        if (seen.has(uniqueKey)) {{
          return;
        }}
        seen.add(uniqueKey);
        count += 1;
        bridge.addCandidate(JSON.stringify({{
          sentence_text: sentence,
          matched_term: params.query,
          source_title: event && event.title ? event.title : "",
          source_url: buildUrl(event || {{}}),
          timestamp: event && event.start ? event.start : "",
          video_id: currentVideo,
          raw_payload: event || {{}}
        }}));
        queueNext();
      }}

      function onError(event) {{
        bridge.fail(JSON.stringify(event || {{}}));
      }}

      new QWebChannel(qt.webChannelTransport, function(channel) {{
        bridge = channel.objects.bridge;
        if (window.YG && typeof YG.Widget === "function") {{
          widget = new YG.Widget("widget-root", {{
            width: 1,
            height: 1,
            components: 1,
            events: {{
              onFetchDone: onFetchDone,
              onVideoChange: onVideoChange,
              onCaptionChange: onCaptionChange,
              onError: onError
            }}
          }});
          widget.fetch(params.query, "korean");
        }} else {{
          bridge.fail(JSON.stringify({{message: "YouGlish widget failed to load"}}));
        }}
      }});
    </script>
  </body>
</html>
"""
        page.setHtml(html, QUrl("https://youglish.com/"))
        timeout.start(7000)
        loop.exec()
        timeout.stop()
        if not collected and errors:
            raise ProviderError(f"Widget fetch failed: {errors[-1]}")
        return collected
