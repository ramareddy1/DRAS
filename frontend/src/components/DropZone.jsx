import { useRef, useState } from "react";

export default function DropZone({ label, file, preview, onFile, error }) {
  const inputRef = useRef(null);
  const [drag, setDrag] = useState(false);

  function handle(files) {
    if (files && files[0]) onFile(files[0]);
  }

  return (
    <div className="flex-1">
      <div className="flex items-baseline justify-between mb-2">
        <h3 className="font-medium text-slate-700">{label}</h3>
        {preview && (
          <span className="text-xs text-slate-500">
            {preview.row_count.toLocaleString()} rows · {(preview.size_bytes / 1024).toFixed(0)} KB
          </span>
        )}
      </div>
      <div
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDrag(false);
          handle(e.dataTransfer.files);
        }}
        className={`cursor-pointer rounded-lg border-2 border-dashed p-6 text-center transition
          ${drag ? "border-brand bg-blue-50" : "border-slate-300 bg-white hover:border-brand"}
          ${error ? "border-bad bg-red-50" : ""}`}
      >
        {file ? (
          <div>
            <div className="font-medium text-slate-800 truncate">{file.name}</div>
            <div className="text-xs text-slate-500 mt-1">
              Click or drop to replace
            </div>
          </div>
        ) : (
          <div className="text-slate-500">
            <div className="font-medium">Drop a CSV or XLSX file here</div>
            <div className="text-xs mt-1">or click to browse · max 10MB</div>
          </div>
        )}
        <input
          ref={inputRef}
          type="file"
          accept=".csv,.xlsx,.xls"
          className="hidden"
          onChange={(e) => handle(e.target.files)}
        />
      </div>
      {error && <div className="text-xs text-bad mt-1">{error}</div>}
    </div>
  );
}
