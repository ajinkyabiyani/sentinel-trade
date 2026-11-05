import React from "react";
import ReactDOM from "react-dom/client";
import App from "./app/App.jsx";
import "./styles/styles.css";
import "./styles/theme.css";
import "./styles/tokens.css";
import "./styles/light.css";
import "./styles/terminal.css";
import "./styles/layout.css";

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
