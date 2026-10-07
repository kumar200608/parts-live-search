// App.jsx
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import Layout from "./components/layout/Layout";
import NorthAmericaParts from "./components/screens/NorthAmericaParts";
import CollisionPartsRetailers from "./components/screens/CollisionPartsRetailers";

const App = () => {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/na-parts" element={<Navigate to="/mechanical-parts-retailers" replace />} />
          <Route path="/mechanical-parts-retailers" element={<NorthAmericaParts />} />
          <Route path="/collision-parts-retailers" element={<CollisionPartsRetailers />} />
          <Route path="/" element={<Navigate to="/mechanical-parts-retailers" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
};

export default App;











