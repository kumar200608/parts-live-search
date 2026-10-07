import React, { useCallback, useRef, useState } from "react";
import {
  AlertTriangle,
  BarChart2,
  CheckCircle,
  Clock,
  Download,
  ExternalLink,
  FileText,
  Globe,
  RefreshCw,
  ShoppingCart,
  Sparkles,
  Upload,
  X,
} from "lucide-react";
import toast, { Toaster } from "react-hot-toast";
import lkqIcon from "../../assets/lkq-icon.png";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "";

const COLLISION_RETAILERS = [
  {
    name: "LKQ",
    code: "lkq",
    domain: "lkqonline.com",
    icon: "🚘",
    iconImage: lkqIcon,
    accentColor: "#0B5FFF",
    bgColor: "#eff6ff",
  },
];

const RetailerIcon = ({ retailer, sizeClass = "h-6 w-6", emojiClass = "text-2xl leading-none" }) => {
  if (retailer?.iconImage) {
    return (
      <img
        src={retailer.iconImage}
        alt={`${retailer.name} logo`}
        className={`${sizeClass} object-contain rounded-full`}
      />
    );
  }
  return <span className={emojiClass}>{retailer?.icon || ""}</span>;
};

const CollisionBatchDropZone = ({ onFileSelect, selectedFile, onClear }) => {
  const [isDragging, setIsDragging] = useState(false);
  const inputRef = useRef(null);
  const isAcceptedFile = (name = "") => /\.(csv|xlsx|xls)$/i.test(String(name));

  const handleDrop = useCallback((event) => {
    event.preventDefault();
    setIsDragging(false);
    const file = event.dataTransfer.files?.[0];
    if (file && isAcceptedFile(file.name)) {
      onFileSelect(file);
    } else {
      toast.error("Please drop a valid .csv, .xlsx, or .xls file");
    }
  }, [onFileSelect]);

  const handleInputChange = (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    if (!isAcceptedFile(file.name)) {
      toast.error("Please choose a valid .csv, .xlsx, or .xls file");
      event.target.value = "";
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
        <button type="button" onClick={onClear} className="text-gray-400 hover:text-red-500 transition-colors">
          <X size={18} />
        </button>
      </div>
    );
  }

  return (
    <div
      onClick={() => inputRef.current?.click()}
      onDrop={handleDrop}
      onDragOver={(event) => { event.preventDefault(); setIsDragging(true); }}
      onDragLeave={() => setIsDragging(false)}
      className={`border-2 border-dashed rounded-xl p-8 text-center cursor-pointer transition-all ${
        isDragging
          ? "border-blue-400 bg-blue-50 scale-[1.01]"
          : "border-gray-300 bg-gray-50 hover:border-blue-300 hover:bg-blue-50"
      }`}
    >
      <Upload size={28} className="mx-auto mb-2 text-gray-400" />
      <p className="text-sm font-semibold text-gray-600">Drop your file here or click to browse</p>
      <p className="text-xs text-gray-400 mt-1">
        Required: <code className="bg-gray-100 px-1 rounded">brand_part_number</code>
        {" "}· Optional: <code className="bg-gray-100 px-1 rounded">retailer_name</code> (LKQ)
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

const CollisionBatchProgress = ({ done, total }) => {
  const percentage = total > 0 ? Math.round((done / total) * 100) : 0;
  return (
    <div className="p-4 bg-blue-50 border border-blue-200 rounded-xl space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold text-blue-800">
          {done === total && total > 0 ? "Finalizing…" : `Searching row ${done} of ${total}`}
        </span>
        <span className="text-xs font-bold text-blue-800">{percentage}%</span>
      </div>
      <div className="h-2 bg-blue-100 rounded-full overflow-hidden">
        <div
          className="h-full bg-[#003478] rounded-full transition-all duration-500 ease-out"
          style={{ width: `${percentage}%` }}
        />
      </div>
    </div>
  );
};

const formatElapsed = (seconds) => {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  if (total < 60) return `${total} sec${total !== 1 ? "s" : ""}`;
  const minutes = Math.floor(total / 60);
  const remainingSeconds = total % 60;
  return `${minutes} min${minutes !== 1 ? "s" : ""}${remainingSeconds ? ` ${remainingSeconds} sec${remainingSeconds !== 1 ? "s" : ""}` : ""}`;
};

const CollisionRetailerCard = ({ retailer, loading, result, hasSearched = false }) => {
  const hasResult = !!result;
  const showAwaiting = !hasResult && !loading && !hasSearched;
  const showNoMatches = !hasResult && !loading && hasSearched;
  const showNotFound = hasResult && result.status !== "success";
  const showPrice =
    hasResult &&
    result.status === "success" &&
    result.price &&
    result.price !== "Price not available";

  return (
    <div
      className="rounded-xl border shadow-md overflow-hidden flex flex-col transform-gpu transition-all duration-300 ease-out hover:-translate-y-1 hover:shadow-lg"
      style={{ borderTopColor: retailer.accentColor, borderTopWidth: 4 }}
    >
      <div className="flex items-center justify-between px-4 py-3 bg-white border-b border-gray-100">
        <div className="flex items-center gap-2">
          <RetailerIcon retailer={retailer} sizeClass="h-7 w-7" />
          <div>
            <p className="text-sm font-bold text-gray-800">{retailer.name}</p>
            <p className="text-xs text-gray-400">{retailer.domain}</p>
          </div>
        </div>
        {loading ? (
          <RefreshCw size={16} className="animate-spin text-blue-500" />
        ) : (
          <CheckCircle size={16} className="text-gray-300" />
        )}
      </div>

      <div className="flex-1 p-4 bg-white min-h-[260px]">
        {loading && (
          <div className="flex flex-col items-center justify-center h-full py-10">
            <RefreshCw size={32} className="animate-spin text-blue-400 mb-3" />
            <p className="text-xs text-gray-500">Searching LKQ…</p>
          </div>
        )}

        {showAwaiting && (
          <div className="flex flex-col items-center justify-center h-full text-gray-300 py-10">
            <ShoppingCart size={36} className="opacity-30 mb-2" />
            <p className="text-xs italic">Awaiting search…</p>
          </div>
        )}

        {showNoMatches && (
          <div className="flex flex-col items-center justify-center h-full text-gray-400 py-10">
            <AlertTriangle size={30} className="mb-2 text-amber-400" />
            <p className="text-xs font-semibold">No LKQ matches found</p>
          </div>
        )}

        {!loading && hasResult && (
          <>
            <div className="rounded-lg p-3 mb-3 text-center" style={{ background: retailer.bgColor }}>
              {showNotFound ? (
                <p className="text-sm font-semibold text-gray-400 italic">Part not found</p>
              ) : showPrice ? (
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

            {!showNotFound && result.image_url && (
              <img
                src={result.image_url}
                alt={result.product_title || "LKQ part"}
                className="w-full h-24 object-contain rounded border border-gray-100 mb-2 bg-gray-50"
              />
            )}

            <p className="text-xs font-semibold text-gray-700 leading-tight mb-2 line-clamp-2">
              {result.product_title || "N/A"}
            </p>

            <div className="space-y-2 mt-3">
              <div className="flex items-center justify-between text-xs border-b border-gray-100 pb-1.5">
                <span className="font-medium uppercase tracking-wide text-gray-500">Part #</span>
                <span className="font-semibold text-gray-700 text-right break-all">{result.part_number || "N/A"}</span>
              </div>
              <div className="flex items-center justify-between text-xs border-b border-gray-100 pb-1.5">
                <span className="font-medium uppercase tracking-wide text-gray-500">Condition</span>
                <span className="font-semibold text-gray-700">{result.condition || "N/A"}</span>
              </div>
              <div className="flex items-center justify-between text-xs">
                <span className="font-medium uppercase tracking-wide text-gray-500">Availability</span>
                <span className="font-semibold text-gray-700">
                  {result.availability_message || result.availability || "N/A"}
                </span>
              </div>
            </div>

            {!showNotFound && (result.part_link || result.source_url) && (
              <a
                href={result.part_link || result.source_url}
                target="_blank"
                rel="noopener noreferrer"
                className="mt-3 flex items-center gap-1.5 text-xs font-semibold text-blue-600 hover:text-blue-800 hover:underline transition-colors"
              >
                <ExternalLink size={12} />
                View part on LKQ
              </a>
            )}
          </>
        )}
      </div>
    </div>
  );
};

export default function CollisionPartsRetailers() {
  const [partNumber, setPartNumber] = useState("");
  const [activeTab, setActiveTab] = useState("live"); // "live" | "batch"
  const [isSearching, setIsSearching] = useState(false);
  const [lkqResults, setLkqResults] = useState([]);
  const [lkqCount, setLkqCount] = useState(0);
  const [elapsed, setElapsed] = useState(null);
  const [hasSearched, setHasSearched] = useState(false);
  const [batchFile, setBatchFile] = useState(null);
  const [batchResults, setBatchResults] = useState([]);
  const [batchProgress, setBatchProgress] = useState(null);
  const [batchElapsed, setBatchElapsed] = useState(null);
  const [isBatching, setIsBatching] = useState(false);

  const handleCollisionSearch = async () => {
    const keyword = partNumber.trim();
    if (!keyword) {
      toast.error("Please enter a collision part number");
      return;
    }

    setIsSearching(true);
    setElapsed(null);
    setLkqResults([]);
    setLkqCount(0);
    setHasSearched(false);

    const loadingToast = toast.loading("Searching LKQ…");
    try {
      const response = await fetch(`${API_BASE_URL}/collision-parts/scrape-live`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          part_number: keyword,
        }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload?.error || "Collision search failed");
      }

      const results = Array.isArray(payload.results) ? payload.results : [];
      setLkqResults(results);
      setLkqCount(Number(payload.count ?? results.length) || 0);
      setElapsed(payload.elapsed ?? null);
      setHasSearched(true);

      toast.success(
        `LKQ search complete${results.length ? ` (${results.length} fetched)` : ""}`,
        { id: loadingToast }
      );
    } catch (error) {
      toast.error(error?.message || "Collision search failed", { id: loadingToast });
      setHasSearched(true);
    } finally {
      setIsSearching(false);
    }
  };

  const handleBatchScrape = async () => {
    if (!batchFile) {
      toast.error("Please select a collision batch file first");
      return;
    }

    setIsBatching(true);
    setBatchResults([]);
    setBatchProgress(null);
    setBatchElapsed(null);
    const startedAt = performance.now();
    const formData = new FormData();
    formData.append("file", batchFile);
    const loadingToast = toast.loading("Connecting to LKQ…");
    let receivedDone = false;

    try {
      const response = await fetch(`${API_BASE_URL}/collision-parts/scrape-batch-stream`, {
        method: "POST",
        body: formData,
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.error || "Collision batch search failed");
      }
      if (!response.body) throw new Error("Streaming is not supported by this browser");

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      const processEvent = (chunk) => {
        if (!chunk.startsWith("data: ")) return;
        try {
          const event = JSON.parse(chunk.slice(6));
          if (event.type === "start") {
            const total = Number(event.total || 0);
            setBatchProgress({ done: 0, total });
            const ignoredRows = Number(event.ignored_rows || 0);
            if (ignoredRows > 0) {
              toast.error(
                `Maximum 200 parts allowed. Processing the first 200; ${ignoredRows} additional row${ignoredRows !== 1 ? "s" : ""} will not be searched.`,
                { duration: 7000 }
              );
            }
            toast.loading(`Searching ${total} collision part${total !== 1 ? "s" : ""}…`, { id: loadingToast });
          } else if (event.type === "progress") {
            setBatchProgress({ done: Number(event.done || 0), total: Number(event.total || 0) });
            setBatchResults((previous) => [...previous, event.row || {}]);
          } else if (event.type === "done") {
            receivedDone = true;
            const duration = event.elapsed ?? ((performance.now() - startedAt) / 1000).toFixed(2);
            setBatchElapsed(duration);
            toast.success(
              `Done! ${event.found_count ?? 0}/${event.results_count ?? 0} parts found in ${formatElapsed(duration)}`,
              { id: loadingToast }
            );
          }
        } catch {
          // Ignore malformed individual stream events and continue processing.
        }
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const chunks = buffer.split("\n\n");
        buffer = chunks.pop();
        chunks.forEach(processEvent);
      }
      buffer += decoder.decode();
      if (buffer.trim()) buffer.trim().split("\n\n").forEach(processEvent);

      if (!receivedDone) {
        const duration = ((performance.now() - startedAt) / 1000).toFixed(2);
        setBatchElapsed(duration);
        toast.error("Stream ended unexpectedly. Partial results are shown.", { id: loadingToast });
      }
    } catch (error) {
      toast.error(error?.message || "Collision batch search failed", { id: loadingToast });
    } finally {
      setIsBatching(false);
    }
  };

  const handleDownloadBatchTemplate = async () => {
    const loadingToast = toast.loading("Preparing collision template…");
    try {
      const response = await fetch(`${API_BASE_URL}/collision-parts/batch-template`);
      if (!response.ok) throw new Error("Failed to download collision template");
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "collision_parts_batch_template.xlsx";
      link.click();
      URL.revokeObjectURL(url);
      toast.success("Template downloaded!", { id: loadingToast });
    } catch (error) {
      toast.error(error?.message || "Template download failed", { id: loadingToast });
    }
  };

  const handleExportBatchXlsx = async () => {
    if (!batchResults.length) return;
    const loadingToast = toast.loading("Preparing collision results…");
    try {
      const response = await fetch(`${API_BASE_URL}/collision-parts/export-batch-xlsx`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(batchResults),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.error || "Failed to export collision results");
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `collision_parts_prices_${new Date().toISOString().slice(0, 10)}.xlsx`;
      link.click();
      URL.revokeObjectURL(url);
      toast.success("XLSX downloaded!", { id: loadingToast });
    } catch (error) {
      toast.error(error?.message || "Collision XLSX export failed", { id: loadingToast });
    }
  };

  return (
    <div className="h-full overflow-auto p-6 space-y-5 bg-[#f4f7f9]">
      <Toaster position="top-right" />
      <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-3">
        <div className="flex-1 rounded-2xl border border-[#d9e4f3] bg-gradient-to-r from-[#edf3fb] to-[#f7f9fc] px-4 py-3 shadow-sm">
          <div className="flex items-start gap-3">
            <div className="h-10 w-10 rounded-xl bg-[#0b3f88] flex items-center justify-center shadow-sm shrink-0">
              <Globe size={16} className="text-white" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-[27px] leading-tight font-bold text-[#1f2f46]">
                  Collision Parts Retailers
                </h1>
              </div>
            </div>
          </div>
        </div>
        {elapsed !== null && (
          <div className="flex items-center gap-2 bg-white border border-gray-200 rounded-full px-4 py-1.5 text-sm text-gray-600 shadow-sm">
            <Clock size={14} className="text-blue-400" />
            <span>Completed in <strong>{elapsed}s</strong></span>
          </div>
        )}
      </div>

      <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-4">
        <p className="text-xs font-semibold uppercase tracking-wide text-gray-400 mb-3 flex items-center gap-1.5">
          <ShoppingCart size={14} /> Select Retailers
        </p>
        <div className="flex flex-wrap gap-3">
          {COLLISION_RETAILERS.map((retailer) => {
            const isActive = true;
            return (
              <button
                key={retailer.name}
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
                type="button"
              >
                <span className="text-lg leading-none transition-transform duration-300 ease-out group-hover:scale-125 group-hover:-rotate-6">
                  <RetailerIcon retailer={retailer} sizeClass="h-5 w-5" emojiClass="text-lg leading-none" />
                </span>
                {retailer.name}
                {isActive && <CheckCircle size={14} className="text-blue-200" />}
                <span
                  className="pointer-events-none absolute left-3 right-3 bottom-1 h-0.5 rounded-full origin-left scale-x-0 transition-transform duration-300 ease-out group-hover:scale-x-100"
                  style={{ background: isActive ? "rgba(255,255,255,0.7)" : "var(--accent)" }}
                />
              </button>
            );
          })}
        </div>
      </div>

      <div className="bg-white rounded-xl shadow-sm border border-gray-100 overflow-hidden">
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
            type="button"
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
            type="button"
          >
            <Upload size={16} /> Batch Upload
          </button>
        </div>

        <div className="p-5">
          {activeTab === "live" && (
            <div className="flex items-end gap-3">
              <div className="flex-1">
                <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1.5">
                  Collision Part Number
                </label>
                <input
                  type="text"
                  placeholder="e.g. FO1095231PP"
                  value={partNumber}
                  onChange={(e) => setPartNumber(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && handleCollisionSearch()}
                  className="w-full border border-gray-200 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-[#003478] focus:border-transparent"
                />
              </div>

              <button
                type="button"
                onClick={handleCollisionSearch}
                disabled={isSearching}
                className={`
                  shrink-0 flex items-center justify-center gap-2 px-6 py-2.5 rounded-lg
                  font-bold text-sm text-white transition-all whitespace-nowrap
                  ${isSearching
                    ? "bg-gray-400 cursor-not-allowed"
                    : "bg-[#003478] hover:bg-[#001e3d] shadow-md hover:shadow-lg active:scale-[0.99]"
                  }
                `}
              >
                {isSearching
                  ? <><RefreshCw size={16} className="animate-spin" /> Searching LKQ…</>
                  : <><Sparkles size={16} /> Search</>
                }
              </button>

            </div>
          )}

          {activeTab === "batch" && (
            <div className="space-y-4">
              <CollisionBatchDropZone
                onFileSelect={setBatchFile}
                selectedFile={batchFile}
                onClear={() => {
                  setBatchFile(null);
                  setBatchProgress(null);
                  setBatchResults([]);
                  setBatchElapsed(null);
                }}
              />

              <div className="flex items-start gap-2 rounded-lg border border-amber-300 bg-amber-50 p-3 text-amber-800">
                <AlertTriangle size={16} className="mt-0.5 shrink-0" />
                <p className="text-xs font-semibold">
                  Maximum 200 parts can be searched per batch. If the file contains more than 200 valid rows, only the first 200 will be searched and included in the results Excel file.
                </p>
              </div>

              <div className="flex items-center justify-between gap-3 rounded-lg border border-dashed border-gray-200 bg-gray-50 px-3 py-2.5 text-xs text-gray-500">
                <span>
                  Each row uses the same LKQ catalog search as Collision Live Search. Duplicate part numbers reuse the first lookup.
                </span>
                <button
                  type="button"
                  onClick={handleDownloadBatchTemplate}
                  className="flex shrink-0 items-center gap-1.5 rounded-lg border border-[#003478] px-3 py-1.5 text-xs font-bold text-[#003478] hover:bg-[#003478] hover:text-white transition-colors"
                >
                  <Download size={14} /> Download Excel template
                </button>
              </div>

              <button
                type="button"
                onClick={handleBatchScrape}
                disabled={isBatching || !batchFile}
                className={`w-full flex items-center justify-center gap-2 py-3 rounded-lg font-bold text-sm text-white transition-all ${
                  isBatching || !batchFile
                    ? "bg-gray-400 cursor-not-allowed"
                    : "bg-[#003478] hover:bg-[#001e3d] shadow-md hover:shadow-lg active:scale-[0.99]"
                }`}
              >
                {isBatching ? (
                  <>
                    <RefreshCw size={16} className="animate-spin" />
                    {batchProgress
                      ? `Searching ${batchProgress.done} / ${batchProgress.total} rows…`
                      : "Connecting…"}
                  </>
                ) : (
                  <><BarChart2 size={16} /> Start Batch Search</>
                )}
              </button>

              <p className="text-xs text-gray-400 text-center">
                LKQ is searched by <code className="bg-gray-100 px-1 rounded">brand_part_number</code>; price and details are returned only for an exact part-number match. Letter suffixes identify different parts.
              </p>

              {isBatching && batchProgress && (
                <CollisionBatchProgress done={batchProgress.done} total={batchProgress.total} />
              )}

              {batchResults.length > 0 && (
                <div className="rounded-xl border border-green-200 bg-green-50 p-4 space-y-3">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div className="flex items-center gap-2 text-green-800">
                      <CheckCircle size={18} />
                      <span className="font-bold text-sm">
                        {isBatching
                          ? `${batchResults.length} row${batchResults.length !== 1 ? "s" : ""} so far…`
                          : `Batch complete — ${batchResults.length} row${batchResults.length !== 1 ? "s" : ""}${batchElapsed !== null ? ` in ${formatElapsed(batchElapsed)}` : ""}`}
                      </span>
                    </div>
                    <button
                      type="button"
                      onClick={handleExportBatchXlsx}
                      disabled={isBatching}
                      className="flex items-center gap-1.5 bg-[#003478] disabled:bg-gray-400 text-white px-4 py-2 rounded-lg text-xs font-bold hover:bg-[#001e3d] transition-colors"
                    >
                      <Download size={14} /> Download XLSX
                    </button>
                  </div>

                  <div className="overflow-x-auto rounded-lg border border-green-200 bg-white">
                    <table className="min-w-full text-xs">
                      <thead>
                        <tr className="bg-gray-50 text-gray-500 uppercase tracking-wide">
                          {["Retailer", "Part Number", "Price", "Product", "Condition", "Availability"].map((heading) => (
                            <th key={heading} className="px-3 py-2 text-left font-semibold">{heading}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {batchResults.slice(0, 10).map((row, index) => (
                          <tr key={`${row.brand_part_number || "part"}-${index}`} className={index % 2 === 0 ? "bg-white" : "bg-gray-50"}>
                            <td className="px-3 py-1.5 font-medium text-gray-700">LKQ</td>
                            <td className="px-3 py-1.5 text-gray-600 font-mono">{row.brand_part_number || "—"}</td>
                            <td className="px-3 py-1.5 font-bold text-green-700">{row.part_price ? `$${row.part_price}` : "—"}</td>
                            <td className="px-3 py-1.5 text-gray-600 max-w-[220px] truncate" title={row.product_title || ""}>{row.product_title || "—"}</td>
                            <td className="px-3 py-1.5 text-gray-600">{row.condition || "—"}</td>
                            <td className="px-3 py-1.5 text-gray-600">{row.availability || "—"}</td>
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

      {activeTab === "live" && <div>
        <p className="text-xs font-semibold uppercase tracking-wide text-gray-400 mb-3 flex items-center gap-1.5">
          <ShoppingCart size={14} /> Results per Retailer
        </p>
        {lkqResults.length > 0 && (
          <p className="text-sm text-gray-500 mb-3">
            LKQ - {lkqCount} total match{lkqCount !== 1 ? "es" : ""} ({lkqResults.length} fetched)
          </p>
        )}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-4">
          {lkqResults.length > 0
            ? lkqResults.map((result, idx) => (
                <CollisionRetailerCard
                  key={`${result.part_number || "lkq"}-${idx}`}
                  retailer={COLLISION_RETAILERS[0]}
                  loading={false}
                  result={result}
                />
              ))
            : (
              <CollisionRetailerCard
                retailer={COLLISION_RETAILERS[0]}
                loading={isSearching}
                result={null}
                hasSearched={hasSearched}
              />
            )}
        </div>
      </div>}
    </div>
  );
}
