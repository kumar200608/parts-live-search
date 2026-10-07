import React, { useState, useEffect } from "react";
import { useLocation } from "react-router-dom";
import Ford from "../../assets/ford.svg";
import { User } from 'lucide-react';

const ROUTE_LABELS = {
  "/na-parts": { label: "NA Parts", sub: "US Retailers" },
  "/mechanical-parts-retailers": { label: "NA Parts", sub: "US Retailers" },
  "/collision-parts-retailers": { label: "NA Parts", sub: "US Retailers" },
};

function Header() {
  const location = useLocation();
  const [userName, setUserName] = useState("");
  const [userLoaded, setUserLoaded] = useState(false);

  useEffect(() => {
    fetch("/api/me")
      .then((res) => res.json())
      .then((data) => {
        if (data.email) {
          // IAP format: "accounts.google.com:user@ford.com" → "USER"
          const email = data.email.includes(":")
            ? data.email.split(":")[1]
            : data.email;
          setUserName(email.split("@")[0].toUpperCase());
        }
      })
      .catch(() => {})
      .finally(() => setUserLoaded(true));
  }, []);

  const routeInfo = ROUTE_LABELS[location.pathname];

  return (
    <header className="flex bg-[#000040] text-white py-3 shadow-lg w-full h-[8vh] items-center justify-between border-b border-white/10 px-6 shrink-0">
      {/* Left Side: Brand */}
      <div className="flex flex-row items-center gap-2">
        <img src={Ford} alt="Logo" className="w-20 brightness-0 invert" />
        {routeInfo && (
          <div className="flex items-center gap-1.5 border-l border-white/20 pl-4 ml-1">
            <span className="text-sm font-semibold text-white/80">{routeInfo.label}</span>
            <span className="text-xs text-white/40">{routeInfo.sub}</span>
          </div>
        )}
      </div>

      {/* Right Side: User Info */}
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-2 bg-white/5 px-4 py-2 rounded-full border border-white/10">
          <User size={16} className="text-blue-300" />
          <span className="text-sm font-bold tracking-wide">
            {!userLoaded ? "Loading…" : userName ? `Welcome, ${userName}` : "Local session"}
          </span>
        </div>
      </div>
    </header>
  );
}

export default Header;
