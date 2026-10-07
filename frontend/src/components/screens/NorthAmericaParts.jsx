// components/screens/NorthAmericaParts.jsx
// North America Price Intelligence — AutoZone, O'Reilly, Advance Auto, Amazon, NAPA, Summit Racing, Walmart, CarParts
// Follows the same design language as the existing Parts AI UI.

import React, { useState, useRef, useCallback, useEffect } from "react";
import {
  Search, RefreshCw, Upload, Download, CheckCircle, XCircle,
  Clock, FileText, Globe, Sparkles, Package,
  Star, ShoppingCart, Tag, BarChart2, X, Calendar, Info, ExternalLink,
} from "lucide-react";
import toast, { Toaster } from "react-hot-toast";
import autozoneIcon from "../../assets/autozone-icon.png";
import advanceAutoIcon from "../../assets/advanceauto-icon.png";
import amazonIcon from "../../assets/amazon-icon.png";
import napaIcon from "../../assets/napa-icon.png";
import oreillyIcon from "../../assets/oreilly-icon.png";
import walmartIcon from "../../assets/walmart-icon.png";
import carPartsIcon from "../../assets/carparts-icon.png";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "";

// ---------------------------------------------------------------------------
// Retailer registry (matches RETAILER_CONFIG in na_retailers_scraper.py)
// ---------------------------------------------------------------------------
const RETAILERS = [
  { name: "AutoZone",      code: "autozone",     domain: "autozone.com",              icon: "🏪", iconImage: autozoneIcon, accentColor: "#E3000F", bgColor: "#fff5f5", currency: "USD" },
  { name: "O'Reilly",      code: "oreilly",      domain: "oreillyauto.com",           icon: "🛞", iconImage: oreillyIcon, accentColor: "#00923F", bgColor: "#f0faf3", currency: "USD" },
  { name: "Advance Auto",  code: "advanceauto",  domain: "advanceautoparts.com",      icon: "🔩", iconImage: advanceAutoIcon, accentColor: "#DA291C", bgColor: "#fff5f5", currency: "USD" },
  { name: "Amazon",        code: "amazon",       domain: "amazon.com",                icon: "📦", iconImage: amazonIcon, accentColor: "#FF9900", bgColor: "#fffaf0", currency: "USD" },
  { name: "NAPA",          code: "napa",         domain: "napaonline.com",            icon: "🧰", iconImage: napaIcon, accentColor: "#0046AD", bgColor: "#f0f5ff", currency: "USD" },
  { name: "Summit Racing", code: "summitracing", domain: "summitracing.com",          icon: "🏁", accentColor: "#CE1126", bgColor: "#fff5f5", currency: "USD" },
  { name: "Walmart",       code: "walmart",      domain: "walmart.com",               icon: "🛒", iconImage: walmartIcon, accentColor: "#0071CE", bgColor: "#f0f7ff", currency: "USD" },
  { name: "CarParts",      code: "carparts",     domain: "carparts.com",              icon: "🚗", iconImage: carPartsIcon, accentColor: "#2563EB", bgColor: "#eff6ff", currency: "USD" },
];

// ---------------------------------------------------------------------------
// Small reusable primitives (keeps JSX clean without external ui/card deps)
// ---------------------------------------------------------------------------

const Badge = ({ children, variant = "default" }) => {
  const styles = {
    default:  "bg-gray-100 text-gray-700",
    success:  "bg-green-100 text-green-700",
    error:    "bg-red-100 text-red-700",
    warning:  "bg-amber-100 text-amber-700",
    info:     "bg-blue-100 text-blue-700",
  };
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-semibold ${styles[variant]}`}>
      {children}
    </span>
  );
};

const DataRow = ({ icon: Icon, label, value, highlight }) => (
  <div className="flex items-start justify-between gap-2 py-1.5 border-b border-gray-100 last:border-0">
    <div className="flex items-center gap-1.5 text-gray-500 shrink-0">
      {Icon && <Icon size={13} />}
      <span className="text-xs font-medium uppercase tracking-wide">{label}</span>
    </div>
    <span className={`text-xs text-right font-medium ${highlight ? "text-green-700 font-bold" : "text-gray-700"}`}>
      {value || "N/A"}
    </span>
  </div>
);

const RetailerIcon = ({
  retailer,
  imageClassName = "h-6 w-6",
  emojiClassName = "text-2xl leading-none",
}) => {
  if (retailer?.iconImage) {
    return (
      <img
        src={retailer.iconImage}
        alt={`${retailer.name} logo`}
        className={`${imageClassName} object-contain rounded-full`}
      />
    );
  }
  return <span className={emojiClassName}>{retailer?.icon || ""}</span>;
};

// ---------------------------------------------------------------------------
// Retailer Result Card
// ---------------------------------------------------------------------------
const RetailerCard = ({ retailer, result, loading }) => {
  const hasResult = !!result;
  const rawPrice  = result?.price || "";
  const rawStatus = result?.status || "";

  // Helper to check if string is a valid numeric price (e.g. "13.49", "150", "1,299.99")
  const isValidPrice = (priceStr) => {
    if (!priceStr) return false;
    return /^\d+([.,\s]\d+)*$/.test(priceStr.trim());
  };

  // Determine visual display state
  let displayState = "awaiting"; // "loading" | "awaiting" | "price" | "not_available" | "not_found"

  if (loading) {
    displayState = "loading";
  } else if (!hasResult) {
    displayState = "awaiting";
  } else {
    if (rawStatus === "success" && isValidPrice(rawPrice)) {
      displayState = "price";
    } else if (rawPrice === "Price not available") {
      displayState = "not_available";
    } else {
      displayState = "not_found";
    }
  }

  const isErrorForRows = displayState === "not_found";

  const availLower  = (result?.availability || "").toLowerCase();
  const inStock     = availLower.includes("in stock") || availLower.includes("available") || availLower.includes("add to cart");
  const outOfStock  = availLower.includes("out of stock") || availLower.includes("unavailable") || availLower.includes("backorder");

  return (
    <div
      className="rounded-xl border shadow-md overflow-hidden flex flex-col transform-gpu transition-all duration-300 ease-out hover:-translate-y-1 hover:shadow-lg"
      style={{ borderTopColor: retailer.accentColor, borderTopWidth: 4 }}
    >
      {/* Card Header */}
      <div className="flex items-center justify-between px-4 py-3 bg-white border-b border-gray-100">
        <div className="flex items-center gap-2">
          <RetailerIcon retailer={retailer} imageClassName="h-7 w-7" />
          <div>
            <p className="text-sm font-bold text-gray-800">{retailer.name}</p>
            <p className="text-xs text-gray-400">{retailer.domain}</p>
          </div>
        </div>
        {loading && (
          <RefreshCw size={16} className="animate-spin text-blue-500" />
        )}
        {hasResult && !loading && displayState === "price" && (
          <CheckCircle size={16} className="text-green-500" />
        )}
        {hasResult && !loading && displayState === "not_found" && (
          <XCircle size={16} className="text-red-400" />
        )}
      </div>

      {/* Card Body */}
      <div className="flex-1 p-4 bg-white space-y-1 min-h-[260px]">
        {displayState === "awaiting" && (
          <div className="flex flex-col items-center justify-center h-full text-gray-300 py-10">
            <ShoppingCart size={36} className="opacity-30 mb-2" />
            <p className="text-xs italic">Awaiting search…</p>
          </div>
        )}

        {displayState === "loading" && (
          <div className="flex flex-col items-center justify-center h-full py-10">
            <RefreshCw size={32} className="animate-spin text-blue-400 mb-3" />
            <p className="text-xs text-gray-500">Scraping {retailer.name}…</p>
            <p className="text-xs text-gray-400 mt-1">Handling bot protection & extracting…</p>
          </div>
        )}

        {(displayState === "price" || displayState === "not_available" || displayState === "not_found") && (
          <>
            {/* Price - prominent */}
            <div
              className="rounded-lg p-3 mb-3 text-center"
              style={{ background: retailer.bgColor }}
            >
              {displayState === "not_found" ? (
                <p className="text-sm font-semibold text-gray-400 italic">Part not found</p>
              ) : displayState === "price" ? (
                <>
                  <p className="text-xs text-gray-500 uppercase tracking-wide mb-0.5">Price</p>
                  <p className="text-2xl font-black text-gray-800">
                    <span className="text-sm font-normal text-gray-500 mr-1">$</span>
                    {result.price}
                    <span className="text-sm font-normal text-gray-500 ml-1">USD</span>
                  </p>
                </>
              ) : (
                <p className="text-sm font-semibold text-gray-400 italic">Price not available</p>
              )}
            </div>

            {/* Product title */}
            {!isErrorForRows && result?.product_title && result.product_title !== "N/A" && (
              <p className="text-xs font-semibold text-gray-700 leading-tight mb-2 line-clamp-2">
                {result.product_title}
              </p>
            )}

            {/* Data rows */}
            <DataRow
              icon={Tag}
              label="MSRP"
              value={isErrorForRows ? "N/A" : result?.rrp_message}
            />
            <DataRow
              icon={Tag}
              label="Sale/Regular"
              value={isErrorForRows ? "N/A" : result?.sale_and_regular_price}
            />
            <DataRow
              icon={Star}
              label="Rating"
              value={isErrorForRows ? "N/A" : result?.rating}
            />
            <DataRow
              icon={Package}
              label="Pack Size"
              value={isErrorForRows ? "N/A" : (result?.pack_size ? `× ${result.pack_size}` : "N/A")}
            />
            <DataRow
              icon={Globe}
              label="Delivery"
              value={isErrorForRows ? "N/A" : result?.delivery_message}
            />
            <DataRow
              icon={ShoppingCart}
              label="Store Pickup"
              value={isErrorForRows ? "N/A" : result?.in_store_pickup}
            />
            <DataRow
              icon={Info}
              label="Availability Msg"
              value={isErrorForRows ? "N/A" : result?.availability_message}
            />

            {/* Availability badge */}
            <div className="pt-2">
              {isErrorForRows ? (
                <Badge variant="default">N/A</Badge>
              ) : inStock ? (
                <Badge variant="success">
                  <CheckCircle size={10} /> In Stock
                </Badge>
              ) : outOfStock ? (
                <Badge variant="error">
                  <XCircle size={10} /> Out of Stock
                </Badge>
              ) : (
                <Badge variant="default">{result?.availability || "Unknown"}</Badge>
              )}
            </div>

            {/* Search URL link — hidden when the price is not available */}
            {result?.search_url && displayState !== "not_available" && (
              <a
                href={result.search_url}
                target="_blank"
                rel="noopener noreferrer"
                className="mt-2 flex items-center gap-1.5 text-xs text-blue-500 hover:text-blue-700 hover:underline transition-colors"
              >
                <ExternalLink size={11} />
                View on {retailer.name}
              </a>
            )}
          </>
        )}
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Batch File Drag-Drop Zone
// ---------------------------------------------------------------------------
const CsvDropZone = ({ onFileSelect, selectedFile, onClear }) => {
  const [isDragging, setIsDragging] = useState(false);
  const inputRef = useRef(null);
  const isAcceptedFile = (fileName = "") => {
    const lower = String(fileName).toLowerCase();
    return lower.endsWith(".csv") || lower.endsWith(".xlsx") || lower.endsWith(".xls");
  };

  const handleDrop = useCallback((e) => {
    e.preventDefault();
    setIsDragging(false);
    const file = e.dataTransfer.files?.[0];
    if (file && isAcceptedFile(file.name)) {
      onFileSelect(file);
    } else {
      toast.error("Please drop a valid .csv, .xlsx, or .xls file");
    }
  }, [onFileSelect]);

  const handleDragOver = (e) => { e.preventDefault(); setIsDragging(true); };
  const handleDragLeave = () => setIsDragging(false);

  const handleInputChange = (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (!isAcceptedFile(file.name)) {
      toast.error("Please choose a valid .csv, .xlsx, or .xls file");
      return;
    }
    onFileSelect(file);
  };

  if (selectedFile) {
    return (
      <div className="flex items-center gap-3 p-4 border-2 border-green-300 bg-green-50 rounded-xl">
        <FileText size={20} className="text-green-600 shrink-0" />
        <div className="flex-1 min-w-0">
          <p className="text-sm font-semibold text-green-800 truncate">{selectedFile.name}</p>
          <p className="text-xs text-green-600">{(selectedFile.size / 1024).toFixed(1)} KB</p>
        </div>
        <button
          onClick={onClear}
          className="text-gray-400 hover:text-red-500 transition-colors"
        >
          <X size={18} />
        </button>
      </div>
    );
  }

  return (
    <div
      onClick={() => inputRef.current?.click()}
      onDrop={handleDrop}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      className={`
        border-2 border-dashed rounded-xl p-8 text-center cursor-pointer transition-all
        ${isDragging
          ? "border-blue-400 bg-blue-50 scale-[1.01]"
          : "border-gray-300 bg-gray-50 hover:border-blue-300 hover:bg-blue-50"
        }
      `}
    >
      <Upload size={28} className="mx-auto mb-2 text-gray-400" />
      <p className="text-sm font-semibold text-gray-600">Drop your file here or click to browse</p>
      <p className="text-xs text-gray-400 mt-1">
        Required columns: <code className="bg-gray-100 px-1 rounded">retailer_name</code>,{" "}
        <code className="bg-gray-100 px-1 rounded">brand_part_number</code>
        {" "}· Optional: <code className="bg-gray-100 px-1 rounded">brand_name</code>,{" "}
        <code className="bg-gray-100 px-1 rounded">mli</code> (both sharpen the query),{" "}
        <code className="bg-gray-100 px-1 rounded">part_price</code>
      </p>
      <input
        ref={inputRef}
        type="file"
        accept=".csv,.xlsx,.xls"
        className="hidden"
        onChange={handleInputChange}
      />
    </div>
  );
};

// ---------------------------------------------------------------------------
// Progress Bar — live batch progress (streaming)
// ---------------------------------------------------------------------------
const ProgressBar = ({ done, total }) => {
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  return (
    <div className="p-4 bg-blue-50 border border-blue-200 rounded-xl space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold text-blue-800">
          {done === total && total > 0
            ? "Finalizing…"
            : `Scraping row ${done} of ${total}`}
        </span>
        <span className="text-xs font-bold text-blue-800">{pct}%</span>
      </div>
      <div className="h-2 bg-blue-100 rounded-full overflow-hidden">
        <div
          className="h-full bg-[#003478] rounded-full transition-all duration-500 ease-out"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Time formatter
// ---------------------------------------------------------------------------
const formatElapsed = (seconds) => {
  const total = Math.round(parseFloat(seconds));
  if (total < 60) return `${total} sec${total !== 1 ? "s" : ""}`;
  const mins  = Math.floor(total / 60);
  const hours = Math.floor(mins  / 60);
  const remMins = mins % 60;
  if (hours === 0) return `${mins} min${mins !== 1 ? "s" : ""}`;
  return `${hours} hr${hours !== 1 ? "s" : ""}${remMins > 0 ? ` ${remMins} min${remMins !== 1 ? "s" : ""}` : ""}`;
};

// Compact live-ticking format (e.g. "12s", "1m 05s") for the running timer display
const formatLiveTimer = (seconds) => {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  if (total < 60) return `${total}s`;
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  return `${mins}m ${String(secs).padStart(2, "0")}s`;
};

export default function NorthAmericaParts() {
  // --- State ---
  const [activeTab, setActiveTab]             = useState("live");        // "live" | "batch"
  const [selectedRetailers, setSelected]      = useState(["AutoZone", "O'Reilly", "Advance Auto", "Amazon", "NAPA", "Summit Racing", "Walmart", "CarParts"]);

  // Live search
  const [partNumber, setPartNumber]       = useState("");
  const [brandName, setBrandName]         = useState("");
  const [liveResults, setLiveResults]     = useState({});
  const [loadingRetailers, setLoadingR]   = useState({});
  const [liveElapsed, setLiveElapsed]     = useState(null);
  const [isSearching, setIsSearching]     = useState(false);
  const [liveRunSeconds, setLiveRunSeconds] = useState(0);   // live-ticking elapsed while searching
  const liveTimerRef = useRef(null);                         // setInterval id
  const liveStartRef = useRef(null);                         // performance.now() at run start

  // Batch upload
  const [csvFile, setCsvFile]             = useState(null);
  const [batchResults, setBatchResults]   = useState([]);
  const [batchElapsed, setBatchElapsed]   = useState(null);
  const [isBatching, setIsBatching]       = useState(false);
  const [batchProgress, setBatchProgress] = useState(null);
  const [batchRunSeconds, setBatchRunSeconds] = useState(0); // live-ticking elapsed while batching
  const batchTimerRef = useRef(null);                        // setInterval id
  const batchStartRef = useRef(null);                        // performance.now() at run start
  const batchAbortRef = useRef(null);                        // AbortController to cancel an in-flight batch fetch

  // ---------------------------------------------------------------------------
  // Elapsed-time timers — tick ~4x/sec while a run is in progress so the user
  // sees time accumulating live; the final total is frozen from the backend
  // `done` event (or a client-measured fallback) once the run finishes.
  // ---------------------------------------------------------------------------
  const startLiveTimer = () => {
    liveStartRef.current = performance.now();
    setLiveRunSeconds(0);
    if (liveTimerRef.current) clearInterval(liveTimerRef.current);
    liveTimerRef.current = setInterval(() => {
      if (liveStartRef.current != null) {
        setLiveRunSeconds((performance.now() - liveStartRef.current) / 1000);
      }
    }, 250);
  };

  const stopLiveTimer = () => {
    if (liveTimerRef.current) {
      clearInterval(liveTimerRef.current);
      liveTimerRef.current = null;
    }
  };

  const startBatchTimer = () => {
    batchStartRef.current = performance.now();
    setBatchRunSeconds(0);
    if (batchTimerRef.current) clearInterval(batchTimerRef.current);
    batchTimerRef.current = setInterval(() => {
      if (batchStartRef.current != null) {
        setBatchRunSeconds((performance.now() - batchStartRef.current) / 1000);
      }
    }, 250);
  };

  const stopBatchTimer = () => {
    if (batchTimerRef.current) {
      clearInterval(batchTimerRef.current);
      batchTimerRef.current = null;
    }
  };

  // Clear any running interval on unmount to avoid leaks
  useEffect(() => {
    return () => {
      if (liveTimerRef.current) clearInterval(liveTimerRef.current);
      if (batchTimerRef.current) clearInterval(batchTimerRef.current);
      // Abort any in-flight batch so the backend stops scraping (and spending
      // SerpApi credits) the moment the user navigates away from this screen.
      if (batchAbortRef.current) batchAbortRef.current.abort();
    };
  }, []);

  // ---------------------------------------------------------------------------
  // Retailer toggle
  // ---------------------------------------------------------------------------
  const toggleRetailer = (name) => {
    setSelected((prev) =>
      prev.includes(name)
        ? prev.length > 1 ? prev.filter((r) => r !== name) : prev   // keep at least 1
        : [...prev, name]
    );
  };

  // ---------------------------------------------------------------------------
  // Live search
  // ---------------------------------------------------------------------------
  const handleLiveSearch = async () => {
    const partNumberValue = partNumber.trim();
    const brandNameValue = brandName.trim();
    const selectedRetailersSnapshot = [...selectedRetailers];

    if (!partNumberValue) {
      toast.error("Please enter a part number");
      return;
    }
    if (!brandNameValue) {
      toast.error("Please enter a brand name");
      return;
    }
    if (selectedRetailersSnapshot.length === 0) {
      toast.error("Please select at least one retailer");
      return;
    }

    setIsSearching(true);
    setLiveResults({});
    setLiveElapsed(null);
    startLiveTimer();

    // Pre-set loading state for all selected retailers
    const initLoading = {};
    selectedRetailersSnapshot.forEach((r) => { initLoading[r] = true; });
    setLoadingR(initLoading);

    const loadingToast = toast.loading(
      `Scraping ${selectedRetailersSnapshot.length} retailer${selectedRetailersSnapshot.length > 1 ? "s" : ""}…`
    );

    let receivedDone = false;
    const streamedResultsByRetailer = {};

    try {
      const response = await fetch(`${API_BASE_URL}/na-parts/scrape-live-stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          part_number: partNumberValue,
          brand_name: brandNameValue,
          retailers: selectedRetailersSnapshot,
        }),
      });

      if (!response.ok) {
        let errMsg = "Live scrape failed";
        try { const err = await response.json(); errMsg = err.error || errMsg; } catch { /* ignore malformed error response */ }
        throw new Error(errMsg);
      }

      if (!response.body) {
        throw new Error("Streaming is not supported by this browser");
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      const processLiveEventChunk = (chunk) => {
        if (!chunk.startsWith("data: ")) return;
        try {
          const event = JSON.parse(chunk.slice(6));

          if (event.type === "start") {
            toast.loading(
              `Scraping ${event.total} retailer${event.total !== 1 ? "s" : ""}…`,
              { id: loadingToast }
            );
          } else if (event.type === "progress") {
            const res = event.result;
            if (res?.retailer) {
              streamedResultsByRetailer[res.retailer] = res;
              setLiveResults((prev) => ({ ...prev, [res.retailer]: res }));
              setLoadingR((prev) => ({ ...prev, [res.retailer]: false }));
            }
          } else if (event.type === "done") {
            receivedDone = true;
            const elapsed = event.elapsed ?? ((performance.now() - liveStartRef.current) / 1000).toFixed(2);
            setLiveElapsed(elapsed);
            const doneLoading = {};
            selectedRetailersSnapshot.forEach((retailerName) => { doneLoading[retailerName] = false; });
            setLoadingR(doneLoading);

            toast.success(`Done in ${formatElapsed(elapsed)}`, { id: loadingToast });
            if (event.json_file) {
              toast.success("Results JSON saved");
            }
          }
        } catch {
          // ignore malformed individual events
        }
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const parts = buffer.split("\n\n");
        buffer = parts.pop();

        for (const part of parts) {
          processLiveEventChunk(part);
        }
      }
      buffer += decoder.decode();
      if (buffer.trim()) {
        for (const trailingPart of buffer.trim().split("\n\n")) {
          processLiveEventChunk(trailingPart);
        }
      }

      if (!receivedDone) {
        const elapsed = ((performance.now() - liveStartRef.current) / 1000).toFixed(2);
        setLiveElapsed(elapsed);
        const doneLoading = {};
        selectedRetailersSnapshot.forEach((retailerName) => { doneLoading[retailerName] = false; });
        setLoadingR(doneLoading);
        toast.error("Stream ended unexpectedly. Partial results shown below.", { id: loadingToast });
      }
    } catch (error) {
      selectedRetailersSnapshot.forEach((r) => {
        setLoadingR((prev) => ({ ...prev, [r]: false }));
      });
      toast.error(
        error?.message || "Failed to reach backend. Is it running on port 8001?",
        { id: loadingToast }
      );
    } finally {
      stopLiveTimer();
      setIsSearching(false);
    }
  };

  // ---------------------------------------------------------------------------
  // Batch single-retailer flow — streams row-level results via SSE
  // ---------------------------------------------------------------------------
  const handleBatchScrape = async () => {
    if (!csvFile) { toast.error("Please select a batch file first"); return; }

    const controller = new AbortController();
    batchAbortRef.current = controller;
    const stallTimeoutMs = 120000;
    let stallTimer = null;
    let streamStalled = false;
    const resetStallTimer = () => {
      window.clearTimeout(stallTimer);
      stallTimer = window.setTimeout(() => {
        streamStalled = true;
        controller.abort();
      }, stallTimeoutMs);
    };

    setIsBatching(true);
    setBatchResults([]);
    setBatchElapsed(null);
    setBatchProgress(null);
    startBatchTimer();

    const formData = new FormData();
    formData.append("file", csvFile);

    const loadingToast = toast.loading("Connecting to backend…");
    let receivedDone = false;

    try {
      resetStallTimer();
      const response = await fetch(`${API_BASE_URL}/na-parts/scrape-batch-stream`, {
        method: "POST",
        body: formData,
        signal: controller.signal,
      });

      if (!response.ok) {
        let errMsg = "Batch scrape failed";
        try { const err = await response.json(); errMsg = err.error || errMsg; } catch { /* ignore malformed error response */ }
        throw new Error(errMsg);
      }

      if (!response.body) {
        throw new Error("Streaming is not supported by this browser");
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      const processBatchEventChunk = (chunk) => {
        if (!chunk.startsWith("data: ")) return;
        try {
          const event = JSON.parse(chunk.slice(6));

          if (event.type === "start") {
            const total = Number(event.total || 0);
            setBatchProgress({ done: 0, total });
            toast.loading(`Scraping ${total} row${total !== 1 ? "s" : ""}…`, { id: loadingToast });
          } else if (event.type === "progress") {
            const row = event.row || {};
            setBatchProgress((prev) =>
              prev
                ? { ...prev, done: event.done }
                : { done: event.done, total: event.total }
            );
            setBatchResults((prev) => [
              ...prev,
              {
                ...row,
                part_price: row.part_price || "",
                found: Boolean(row.found),
                error: row.error || "",
              },
            ]);
          } else if (event.type === "done") {
            receivedDone = true;
            const elapsed = event.elapsed ?? ((performance.now() - batchStartRef.current) / 1000).toFixed(2);
            setBatchElapsed(elapsed);
            toast.success(
              `Done! ${event.found_count ?? 0}/${event.results_count ?? 0} prices found in ${formatElapsed(elapsed)}`,
              { id: loadingToast }
            );
          }
        } catch {
          // ignore malformed individual events
        }
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        resetStallTimer();
        buffer += decoder.decode(value, { stream: true });

        const parts = buffer.split("\n\n");
        buffer = parts.pop();

        for (const part of parts) {
          processBatchEventChunk(part);
        }
      }
      buffer += decoder.decode();
      if (buffer.trim()) {
        for (const trailingPart of buffer.trim().split("\n\n")) {
          processBatchEventChunk(trailingPart);
        }
      }

      if (!receivedDone) {
        const elapsed = ((performance.now() - batchStartRef.current) / 1000).toFixed(2);
        setBatchElapsed(elapsed);
        toast.error("Stream ended unexpectedly. Partial results shown below.", { id: loadingToast });
      }
    } catch (error) {
      if (error?.name === "AbortError") {
        if (streamStalled) {
          toast.error("Batch connection stalled and was stopped. Partial results are kept below.", {
            id: loadingToast,
            duration: 5000,
          });
        } else {
          toast("Batch stopped — no further SerpApi credits used. Partial results kept below.", {
            id: loadingToast, icon: "🛑", duration: 5000,
          });
        }
      } else {
        toast.error(
          error?.message || "Batch scrape failed. Check backend logs.",
          { id: loadingToast }
        );
      }
    } finally {
      window.clearTimeout(stallTimer);
      stopBatchTimer();
      setIsBatching(false);
      batchAbortRef.current = null;
    }
  };

  // Abort the in-flight batch fetch — closes the connection so the backend stops
  // scraping and no more SerpApi credits are spent.
  const handleStopBatch = () => {
    if (batchAbortRef.current) batchAbortRef.current.abort();
  };

  // ---------------------------------------------------------------------------
  // Export single-retailer batch results as XLSX (backend-generated)
  // ---------------------------------------------------------------------------
  const handleExportBatchXlsx = async () => {
    if (batchResults.length === 0) return;

    const loadingToast = toast.loading("Preparing XLSX download…");
    try {
      const response = await fetch(`${API_BASE_URL}/na-parts/export-batch-xlsx`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(batchResults),
      });
      if (!response.ok) {
        let errMsg = "Failed to export XLSX";
        try { const err = await response.json(); errMsg = err.error || errMsg; } catch { /* ignore malformed error response */ }
        throw new Error(errMsg);
      }

      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `na_parts_prices_${new Date().toISOString().slice(0, 10)}.xlsx`;
      link.click();
      URL.revokeObjectURL(url);
      toast.success("XLSX downloaded!", { id: loadingToast });
    } catch (error) {
      toast.error(error?.message || "XLSX export failed", { id: loadingToast });
    }
  };

  // ---------------------------------------------------------------------------
  // Download a blank Excel template for the batch upload
  // ---------------------------------------------------------------------------
  const handleDownloadTemplate = async () => {
    const loadingToast = toast.loading("Preparing template…");
    try {
      const response = await fetch(`${API_BASE_URL}/na-parts/batch-template`);
      if (!response.ok) {
        let errMsg = "Failed to download template";
        try { const err = await response.json(); errMsg = err.error || errMsg; } catch { /* ignore malformed error response */ }
        throw new Error(errMsg);
      }

      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "na_parts_batch_template.xlsx";
      link.click();
      URL.revokeObjectURL(url);
      toast.success("Template downloaded!", { id: loadingToast });
    } catch (error) {
      toast.error(error?.message || "Template download failed", { id: loadingToast });
    }
  };

  // ---------------------------------------------------------------------------
  // Export live search results as CSV (client-side)
  // ---------------------------------------------------------------------------
  const handleExportLiveCSV = () => {
    if (Object.keys(liveResults).length === 0) return;
    const columns = [
      "Part Number", "Brand Name", "Retailer", "Status",
      "Product Title", "Price", "Currency", "RRP Message",
      "Sale & Regular Price", "Rating", "Availability",
      "Availability Message", "Delivery Message", "Store Pickup",
      "Pack Size", "Search URL",
    ];
    const rows = Object.values(liveResults).map((res) =>
      [
        partNumber,
        brandName,
        res.retailer      || "",
        res.status         || "",
        res.product_title  || "",
        res.price          || "",
        res.currency       || "",
        res.rrp_message    || "",
        res.sale_and_regular_price || "",
        res.rating         || "",
        res.availability   || "",
        res.availability_message || "",
        res.delivery_message || "",
        res.in_store_pickup || "",
        res.pack_size      || "",
        res.search_url     || "",
      ].map((v) => `"${String(v).replace(/"/g, '""')}"`).join(",")
    );
    const csvContent = [columns.join(","), ...rows].join("\n");
    const blob = new Blob([csvContent], { type: "text/csv" });
    const url  = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href  = url;
    link.download = `na_parts_live_${partNumber.trim().replace(/\s+/g, "_")}_${new Date().toISOString().slice(0, 10)}.csv`;
    link.click();
    URL.revokeObjectURL(url);
    toast.success("CSV downloaded!");
  };

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------
  return (
    <div className="h-full overflow-auto p-6 space-y-5 bg-[#f4f7f9]">
      <Toaster position="top-right" />

      {/* ─── Page Header ─── */}
      <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-3">
        <div className="flex-1 rounded-2xl border border-[#d9e4f3] bg-gradient-to-r from-[#edf3fb] to-[#f7f9fc] px-4 py-3 shadow-sm">
          <div className="flex items-start gap-3">
            <div className="h-10 w-10 rounded-xl bg-[#0b3f88] flex items-center justify-center shadow-sm shrink-0">
              <Globe size={16} className="text-white" />
            </div>
            <div>
              <h1 className="text-[27px] leading-tight font-bold text-[#1f2f46]">
                North America Price Intelligence
              </h1>
              <p className="text-sm text-[#53657d] mt-0.5 flex items-center gap-1.5">
                <Sparkles size={13} className="text-[#5f7ea9]" />
                Real-time competitor pricing across North American retailers via AI-powered web scraping
              </p>
            </div>
          </div>
        </div>
        {activeTab === "live" && (isSearching || liveElapsed != null) && (
          <div className="flex items-center gap-2 bg-white border border-gray-200 rounded-full px-4 py-1.5 text-sm text-gray-600 shadow-sm">
            <Clock size={14} className={isSearching ? "text-blue-500 animate-pulse" : "text-blue-400"} />
            {isSearching
              ? <span>Elapsed <strong className="tabular-nums">{formatLiveTimer(liveRunSeconds)}</strong></span>
              : <span>Completed in <strong>{formatElapsed(liveElapsed)}</strong></span>}
          </div>
        )}
        {activeTab === "batch" && (isBatching || batchElapsed != null) && (
          <div className="flex items-center gap-2 bg-white border border-gray-200 rounded-full px-4 py-1.5 text-sm text-gray-600 shadow-sm">
            <Clock size={14} className={isBatching ? "text-blue-500 animate-pulse" : "text-blue-400"} />
            {isBatching
              ? <span>Elapsed <strong className="tabular-nums">{formatLiveTimer(batchRunSeconds)}</strong></span>
              : <span>Batch done in <strong>{formatElapsed(batchElapsed)}</strong></span>}
          </div>
        )}
      </div>

      {/* ─── Retailer Selector ─── */}
      <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-4">
        <p className="text-xs font-semibold uppercase tracking-wide text-gray-400 mb-3 flex items-center gap-1.5">
          <ShoppingCart size={14} /> Select Retailers
        </p>
        <div className="flex flex-wrap gap-3">
          {RETAILERS.map((retailer) => {
            const isActive = selectedRetailers.includes(retailer.name);
            return (
              <button
                key={retailer.name}
                onClick={() => toggleRetailer(retailer.name)}
                style={{ "--accent": retailer.accentColor }}
                className={`
                  group relative flex items-center gap-2 px-4 py-2 rounded-lg border-2 font-semibold text-sm
                  transition-all duration-300 ease-out select-none transform-gpu will-change-transform
                  hover:-translate-y-1 hover:shadow-lg
                  active:translate-y-0 active:scale-95
                  ${isActive
                    ? "border-[#003478] bg-[#003478] text-white shadow-md"
                    : "border-gray-200 bg-gray-50 text-gray-500 hover:bg-white hover:[border-color:var(--accent)] hover:[color:var(--accent)]"
                  }
                `}
              >
                <span className="text-lg leading-none transition-transform duration-300 ease-out group-hover:scale-125 group-hover:-rotate-6">
                  <RetailerIcon retailer={retailer} imageClassName="h-5 w-5" emojiClassName="text-lg leading-none" />
                </span>
                {retailer.name}
                {isActive && <CheckCircle size={14} className="text-blue-200" />}
                {/* brand-color underline that sweeps in on hover */}
                <span
                  className="pointer-events-none absolute left-3 right-3 bottom-1 h-0.5 rounded-full origin-left scale-x-0 transition-transform duration-300 ease-out group-hover:scale-x-100"
                  style={{ background: isActive ? "rgba(255,255,255,0.7)" : "var(--accent)" }}
                />
              </button>
            );
          })}
        </div>
      </div>

      {/* ─── Mode Tabs ─── */}
      <div className="bg-white rounded-xl shadow-sm border border-gray-100 overflow-hidden">
        {/* Tab bar */}
        <div className="flex border-b border-gray-100">
          <button
            onClick={() => setActiveTab("live")}
            className={`
              flex-1 flex items-center justify-center gap-2 py-3 text-sm font-semibold
              transition-colors duration-150
              ${activeTab === "live"
                ? "bg-[#003478] text-white"
                : "text-gray-500 hover:bg-gray-50"
              }
            `}
          >
            <Sparkles size={16} /> Live Search
          </button>
          <button
            onClick={() => setActiveTab("batch")}
            className={`
              flex-1 flex items-center justify-center gap-2 py-3 text-sm font-semibold
              transition-colors duration-150
              ${activeTab === "batch"
                ? "bg-[#003478] text-white"
                : "text-gray-500 hover:bg-gray-50"
              }
            `}
          >
            <Upload size={16} /> Batch Upload
          </button>
        </div>

        <div className="p-5">
          {/* ── Live Search Panel ── */}
          {activeTab === "live" && (
            <div className="space-y-4">
              <div className="flex flex-col md:flex-row md:items-end gap-4">
                {/* Part Number */}
                <div className="flex-1">
                  <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1.5">
                    OEM Part Number
                  </label>
                  <input
                    type="text"
                    placeholder="e.g. DG-130, SP-534"
                    value={partNumber}
                    onChange={(e) => setPartNumber(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && handleLiveSearch()}
                    className="w-full border border-gray-200 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-[#003478] focus:border-transparent"
                  />
                </div>

                {/* Brand Name */}
                <div className="flex-1">
                  <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1.5">
                    Brand / Manufacturer
                  </label>
                  <input
                    type="text"
                    placeholder="e.g. Motorcraft, ACDelco, Bosch"
                    value={brandName}
                    onChange={(e) => setBrandName(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && handleLiveSearch()}
                    className="w-full border border-gray-200 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-[#003478] focus:border-transparent"
                  />
                </div>

                {/* Run button */}
                <button
                  onClick={handleLiveSearch}
                  disabled={isSearching}
                  className={`
                    w-full md:w-auto shrink-0 flex items-center justify-center gap-2 py-2.5 px-8 rounded-lg
                    font-bold text-base text-white transition-all
                    ${isSearching
                      ? "bg-gray-400 cursor-not-allowed"
                      : "bg-[#003478] hover:bg-[#001e3d] shadow-md hover:shadow-lg active:scale-[0.99]"
                    }
                  `}
                >
                  {isSearching
                    ? <><RefreshCw size={16} className="animate-spin" /> Scraping across {selectedRetailers.length} retailer{selectedRetailers.length > 1 ? "s" : ""}…</>
                    : <><Sparkles size={16} /> Run Price Comparison</>
                  }
                </button>
              </div>
            </div>
          )}

          {/* ── Single-Retailer Batch Panel ── */}
          {activeTab === "batch" && (
            <div className="space-y-4">
              <CsvDropZone
                onFileSelect={setCsvFile}
                selectedFile={csvFile}
                onClear={() => { setCsvFile(null); setBatchProgress(null); }}
              />

              <div className="flex items-center justify-between gap-3 rounded-lg border border-dashed border-gray-200 bg-gray-50 px-3 py-2.5 text-xs text-gray-500">
                <span>
                  Retailer is read from each row in <code className="bg-gray-100 px-1 rounded">retailer_name</code>. This
                  tab ignores the top retailer toggles.
                </span>
                <button
                  type="button"
                  onClick={handleDownloadTemplate}
                  className="flex shrink-0 items-center gap-1.5 rounded-lg border border-[#003478] px-3 py-1.5 text-xs font-bold text-[#003478] hover:bg-[#003478] hover:text-white transition-colors"
                >
                  <Download size={14} /> Download Excel template
                </button>
              </div>

              <button
                onClick={handleBatchScrape}
                disabled={isBatching || !csvFile}
                className={`
                  w-full flex items-center justify-center gap-2 py-3 rounded-lg
                  font-bold text-sm text-white transition-all
                  ${isBatching || !csvFile
                    ? "bg-gray-400 cursor-not-allowed"
                    : "bg-[#003478] hover:bg-[#001e3d] shadow-md hover:shadow-lg active:scale-[0.99]"
                  }
                `}
              >
                {isBatching
                  ? <>
                      <RefreshCw size={16} className="animate-spin" />
                      {batchProgress
                        ? `Scraping ${batchProgress.done} / ${batchProgress.total} rows…`
                        : "Connecting…"
                      }
                    </>
                  : <>
                      <BarChart2 size={16} />
                      Start Batch Scrape
                    </>
                }
              </button>

              {isBatching && (
                <button
                  onClick={handleStopBatch}
                  className="w-full flex items-center justify-center gap-2 py-2.5 rounded-lg font-bold text-sm text-red-700 border border-red-300 bg-red-50 hover:bg-red-100 transition-colors"
                >
                  <XCircle size={16} /> Stop — don&apos;t use more credits
                </button>
              )}

              <p className="text-xs text-gray-400 text-center">
                Each row is scraped for the retailer in that row, and the output fills <code className="bg-gray-100 px-1 rounded">part_price</code>.
              </p>

              {isBatching && batchProgress && (
                <ProgressBar
                  done={batchProgress.done}
                  total={batchProgress.total}
                />
              )}

              {batchResults.length > 0 && (
                <div className="rounded-xl border border-green-200 bg-green-50 p-4 space-y-3">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2 text-green-800">
                      <CheckCircle size={18} />
                      <span className="font-bold text-sm">
                        {isBatching
                          ? `${batchResults.length} row${batchResults.length !== 1 ? "s" : ""} so far…`
                          : `Batch complete — ${batchResults.length} row${batchResults.length !== 1 ? "s" : ""}`
                        }
                      </span>
                    </div>
                    <button
                      onClick={handleExportBatchXlsx}
                      className="flex items-center gap-1.5 bg-[#003478] text-white px-4 py-2 rounded-lg text-xs font-bold hover:bg-[#001e3d] transition-colors"
                    >
                      <Download size={14} /> Download XLSX
                    </button>
                  </div>

                  <div className="overflow-x-auto rounded-lg border border-green-200 bg-white">
                    <table className="min-w-full text-xs">
                      <thead>
                        <tr className="bg-gray-50 text-gray-500 uppercase tracking-wide">
                          {["Retailer", "Part Number", "Brand", "MLI", "Price", "Status"].map((h) => (
                            <th key={h} className="px-3 py-2 text-left font-semibold">{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {batchResults.slice(0, 10).map((row, i) => (
                          <tr key={i} className={i % 2 === 0 ? "bg-white" : "bg-gray-50"}>
                            <td className="px-3 py-1.5 font-medium text-gray-700">{row.retailer_name || "—"}</td>
                            <td className="px-3 py-1.5 text-gray-600 font-mono">{row.brand_part_number || "—"}</td>
                            <td className="px-3 py-1.5 text-gray-600">{row.brand_name || "—"}</td>
                            <td className="px-3 py-1.5 text-gray-600">{row.mli || "—"}</td>
                            <td className="px-3 py-1.5 font-bold text-green-700">
                              {row.part_price ? `$${row.part_price}` : "—"}
                            </td>
                            <td className="px-3 py-1.5">
                              <Badge variant={row.found ? "success" : "warning"}>
                                {row.found ? "Found" : (row.error || "Not found")}
                              </Badge>
                            </td>
                          </tr>
                        ))}
                        {batchResults.length > 10 && (
                          <tr>
                            <td colSpan={6} className="px-3 py-2 text-center text-gray-400 text-xs italic">
                              +{batchResults.length - 10} more rows · Download XLSX for full results
                            </td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </div>
          )}


        </div>
      </div>

      {/* ─── Live Search Results Grid ─── */}
      {activeTab === "live" && (
        <div>
          <div className="flex items-center justify-between mb-3">
            <p className="text-xs font-semibold uppercase tracking-wide text-gray-400 flex items-center gap-1.5">
              <ShoppingCart size={14} /> Results per Retailer
            </p>
            {Object.keys(liveResults).length > 0 && !isSearching && (
              <button
                onClick={handleExportLiveCSV}
                className="flex items-center gap-1.5 bg-[#003478] text-white px-4 py-2 rounded-lg text-xs font-bold hover:bg-[#001e3d] transition-colors"
              >
                <Download size={14} /> Download CSV
              </button>
            )}
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-4">
            {RETAILERS.filter((r) => selectedRetailers.includes(r.name)).map((retailer) => (
              <RetailerCard
                key={retailer.name}
                retailer={retailer}
                result={liveResults[retailer.name]}
                loading={!!loadingRetailers[retailer.name]}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
