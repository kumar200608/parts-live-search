// components/layout/Sidebar.jsx
import React from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { Shield, Wrench } from 'lucide-react';

const NAV_ITEMS = [
  {
    label:    'Mechanical Parts Retailers',
    subLabel: 'Live Search',
    icon:     Wrench,
    path:     '/mechanical-parts-retailers',
  },
  {
    label:    'Collision Parts',
    subLabel: 'US Retailers',
    icon:     Shield,
    path:     '/collision-parts-retailers',
  },
];

const Sidebar = () => {
  const navigate = useNavigate();
  const location = useLocation();

  return (
    <div className="w-64 bg-[#000040] text-white flex flex-col h-full">
      {/* Logo / App name */}
      <div className="p-6 text-xl font-bold border-b border-white/10 flex items-center gap-3">
        Parts AI
      </div>

      {/* Navigation */}
      <nav className="flex-1 p-4 space-y-1">
        {NAV_ITEMS.map((item) => {
          const isActive = location.pathname === item.path;
          const Icon = item.icon;
          return (
            <button
              key={item.path}
              onClick={() => navigate(item.path)}
              className={`
                w-full flex items-center gap-3 p-3 rounded-md text-left transition-colors
                ${isActive
                  ? 'bg-[#003478] text-white shadow-md'
                  : 'text-gray-400 hover:bg-white/5 hover:text-white'
                }
              `}
            >
              <Icon size={18} className={isActive ? 'text-white' : 'text-gray-400'} />
              <div>
                <p className={`text-sm font-semibold leading-tight ${isActive ? 'text-white' : ''}`}>
                  {item.label}
                </p>
                <p className={`text-xs leading-tight ${isActive ? 'text-blue-200' : 'text-gray-500'}`}>
                  {item.subLabel}
                </p>
              </div>
            </button>
          );
        })}
      </nav>

      {/* Footer links */}
      <div className="p-4 border-t border-white/10 space-y-1">
        <p className="text-xs text-gray-500 text-center">Ford · North America</p>
      </div>
    </div>
  );
};

export default Sidebar;
