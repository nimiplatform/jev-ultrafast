// Renderer entry. app.js reads window.jevTransport once when it loads, so the transport module is
// imported (and evaluated) first; the upstream inspector's app.js and style.css are used unchanged.
import './install-transport.js';
import '../jev_ultrafast/static/style.css';
import '../jev_ultrafast/static/app.js';
