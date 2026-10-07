function send(cfg, el) {
  cfg.headers["x-deployment-id"] = "d1";       // a real header, lowercase-dashed
  cfg["x-amz-user-agent"] = "aws-sdk-js";      // also real, assigned directly
  cfg["font-size"] = "12px";                   // a CSS-ish map key, not a header
  el.attributes["x-out-of-boundaries"] = "";   // Popper.js markup, not a header
  el.dataset["x-chart-id"] = "c1";             // DOM dataset, not a header
  return fetch("/api/v1/x", cfg);
}
