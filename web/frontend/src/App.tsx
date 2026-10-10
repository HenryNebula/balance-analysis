import { NavLink, Route, Routes, Outlet, Link, useLocation } from "react-router-dom"
import { Activity, BookOpenCheck, FlaskConical, Landmark } from "lucide-react"
import { cn } from "cn"
import Accounts from "./pages/Accounts"
import AccountDetail from "./pages/AccountDetail"
import StatementView from "./pages/StatementView"
import Registry from "./pages/Registry"
import Runs from "./pages/Runs"
import RunView from "./pages/RunView"

function Layout() {
  const { pathname } = useLocation()
  const link = (active: boolean) =>
    cn(
      "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
      active
        ? "bg-secondary text-secondary-foreground"
        : "text-muted-foreground hover:bg-muted hover:text-foreground",
    )
  // Accounts stays active on its child routes (account + statement pages)
  const accountsActive = pathname === "/" || pathname.startsWith("/accounts")
  return (
    <div className="min-h-svh">
      <header className="sticky top-0 z-40 border-b bg-background/95 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-6xl items-center gap-6 px-6">
          <Link to="/" className="flex items-center gap-2 font-semibold tracking-tight">
            <BookOpenCheck className="size-4.5 text-primary" />
            statementledger
          </Link>
          <nav className="flex items-center gap-1">
            <NavLink to="/" end className={() => link(accountsActive)}>
              <Landmark className="size-4" /> Accounts
            </NavLink>
            <NavLink to="/registry" className={({ isActive }) => link(isActive)}>
              <FlaskConical className="size-4" /> Registry
            </NavLink>
            <NavLink to="/runs" className={({ isActive }) => link(isActive)}>
              <Activity className="size-4" /> Distill runs
            </NavLink>
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-6 py-8">
        <Outlet />
      </main>
    </div>
  )
}

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Accounts />} />
        <Route path="accounts/:slug" element={<AccountDetail />} />
        <Route path="accounts/:slug/statements/:sid" element={<StatementView />} />
        <Route path="registry" element={<Registry />} />
        <Route path="runs" element={<Runs />} />
      </Route>
      {/* the run view is a full-bleed three-pane workspace, not a document */}
      <Route path="runs/:id" element={<RunView />} />
    </Routes>
  )
}
